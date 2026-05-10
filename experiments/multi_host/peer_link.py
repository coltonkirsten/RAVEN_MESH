"""Federation shim: makes a Core federation-aware without touching `core/`.

How it works
------------
* The shim builds its own aiohttp app from scratch, reusing `CoreState` and
  the unmodified handlers from `core.core`.
* The manifest YAML grows two optional top-level keys:

    peer_cores:
      - name: B                        # logical name of the peer Core
        url: http://127.0.0.1:8001
        peer_secret: env:PEER_AB_SECRET  # shared HMAC, this side <-> peer
        # OR (future): peer_pubkey: <base64-ed25519>

    remote_nodes:
      - id: beta                       # node hosted on a peer Core
        peer: B                         # name in peer_cores
        kind: capability
        surfaces:
          - name: ping
            type: tool
            invocation_mode: request_response
            schema: ../../schemas/echo.json

* `remote_nodes` are folded into `state.nodes_decl` as STUB declarations
  (no `secret` — the peer Core is responsible for verifying the original
  invoker's signature). The standard relationship-edge check still works
  because `state.edges` references node IDs, not connections.

* `/v0/invoke` is replaced with `federated_handle_invoke`, which checks if
  the target node is a `remote_node`. If so, it verifies the invoker's
  signature locally, builds a *peer envelope* wrapping the original, signs
  it with the shared peer HMAC, and POSTs to `/v0/peer/envelope` on the
  destination Core. The peer Core's response is returned verbatim.

* `/v0/peer/envelope` (new) accepts incoming peer envelopes from another
  Core. It verifies the peer HMAC, enforces a nonce-and-timestamp anti-
  replay check, validates the inner envelope's freshness, looks up the
  declared remote-node stub for the inner `from`, and dispatches the inner
  envelope through the standard `_route_invocation` path with the
  signature flagged as pre-verified.

Trust chain
-----------
    alpha @ Core A  --[alpha_secret HMAC]-->  Core A
    Core A          --[peer_AB HMAC]-------->  Core B
    Core B          --[deliver via SSE]----->  beta
    beta            --[beta_secret HMAC]---->  Core B
    Core B          --[HTTP response]------->  Core A
    Core A          --[HTTP response]------->  alpha

Failure modes addressed
-----------------------
* Peer disconnection: HTTP error returned to the calling node.
* Replay across hosts: per-peer nonce cache + clock-skew window.
* Time-skew: configurable bound (`±MESH_PEER_SKEW_SECONDS`).
* Signature chain forgery: peer envelope HMAC binds the entire inner
  envelope (canonical JSON serialization), so swapping payloads invalidates
  the outer signature.
* Cross-host downgrade: the inner envelope's own timestamp is also checked;
  Core B refuses inner envelopes older than the skew window even if the
  outer peer envelope is fresh.
"""
from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import pathlib
import time
import uuid
from typing import Any

import aiohttp
import yaml
from aiohttp import web

from core.core import (
    CoreState,
    _admin_authed,
    _cors_middleware,
    _route_invocation,
    canonical,
    handle_admin_invoke,
    handle_admin_manifest,
    handle_admin_node_status,
    handle_admin_processes,
    handle_admin_reconcile,
    handle_admin_reload,
    handle_admin_restart,
    handle_admin_spawn,
    handle_admin_state,
    handle_admin_stop,
    handle_admin_stream,
    handle_admin_ui_state,
    handle_health,
    handle_introspect,
    handle_register,
    handle_respond,
    handle_stream,
    now_iso,
    sign,
    verify,
)


log = logging.getLogger("mesh.peer_link")

# Operator-tunable knobs.
PEER_SKEW_SECONDS = int(os.environ.get("MESH_PEER_SKEW_SECONDS", "300"))
PEER_NONCE_TTL_SECONDS = int(os.environ.get("MESH_PEER_NONCE_TTL", "600"))
PEER_FORWARD_TIMEOUT = float(os.environ.get("MESH_PEER_FORWARD_TIMEOUT", "35"))


# ---------- federation config ----------


class PeerSpec:
    """One peer-Core entry from the manifest's `peer_cores` list."""

    def __init__(self, name: str, url: str, secret: str):
        self.name = name
        self.url = url.rstrip("/")
        self.secret = secret


class RemoteNodeSpec:
    """One remote-node stub from the manifest's `remote_nodes` list."""

    def __init__(self, node_id: str, peer: str, kind: str, surfaces: list[dict]):
        self.id = node_id
        self.peer = peer
        self.kind = kind
        self.surfaces = surfaces


class FederationState:
    """All federation-specific state. Bolted onto CoreState as `.federation`."""

    def __init__(self):
        self.local_name: str = ""  # this Core's logical name; sent as peer_from
        self.peers: dict[str, PeerSpec] = {}
        self.remote_nodes: dict[str, RemoteNodeSpec] = {}
        # nonce -> expires_at (monotonic seconds). Only one entry per
        # (peer_from, nonce) matters because the HMAC binds peer_from too.
        self._nonce_seen: dict[str, float] = {}
        self._nonce_lock = asyncio.Lock()
        self._http: aiohttp.ClientSession | None = None

    async def http(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession()
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def remember_nonce(self, peer_from: str, nonce: str) -> bool:
        """Return True if nonce is fresh (record it). False if seen recently."""
        key = f"{peer_from}:{nonce}"
        now = time.monotonic()
        async with self._nonce_lock:
            self._gc_nonces(now)
            if key in self._nonce_seen:
                return False
            self._nonce_seen[key] = now + PEER_NONCE_TTL_SECONDS
            return True

    def _gc_nonces(self, now: float) -> None:
        # Cheap O(n) sweep. n is bounded by request rate * TTL.
        expired = [k for k, t in self._nonce_seen.items() if t <= now]
        for k in expired:
            self._nonce_seen.pop(k, None)


def _resolve_secret(spec: str, default_label: str) -> str:
    """Resolve `env:VAR` -> actual secret. Mirrors core's secret resolver."""
    if not spec:
        # Same fallback Core uses, but operator should always set this.
        return hashlib.sha256(f"peer:{default_label}:autogen".encode()).hexdigest()
    if spec.startswith("env:"):
        var = spec[4:]
        val = os.environ.get(var)
        if val:
            return val
        # Auto-generate so demos work even with no env. Print so operator notices.
        val = hashlib.sha256(f"peer:{default_label}:autogen".encode()).hexdigest()
        os.environ[var] = val
        log.warning("peer secret env %s unset; autogenerated", var)
        return val
    return spec


def load_federation(manifest_path: str, state: CoreState) -> FederationState:
    """Read manifest's federation sections + register remote-node stubs.

    Mutates `state` to add remote-node entries to `state.nodes_decl`. Edges
    declared via `relationships:` that reference remote nodes already work
    because Core just compares node-ID strings.
    """
    fed = FederationState()
    raw = yaml.safe_load(pathlib.Path(manifest_path).read_text())
    fed.local_name = raw.get("local_core_name") or raw.get("local_name") or "_self_"

    for entry in raw.get("peer_cores", []) or []:
        name = entry["name"]
        url = entry["url"]
        secret_spec = entry.get("peer_secret") or entry.get("shared_hmac") or ""
        secret = _resolve_secret(secret_spec, f"peer_{fed.local_name}_{name}")
        fed.peers[name] = PeerSpec(name=name, url=url, secret=secret)

    manifest_dir = pathlib.Path(manifest_path).resolve().parent
    for entry in raw.get("remote_nodes", []) or []:
        nid = entry["id"]
        peer = entry["peer"]
        kind = entry.get("kind", "capability")
        if peer not in fed.peers:
            raise ValueError(
                f"remote_node '{nid}' references unknown peer '{peer}'. "
                f"Known peers: {list(fed.peers)}"
            )
        if nid in state.nodes_decl:
            raise ValueError(
                f"remote_node '{nid}' collides with a locally declared node"
            )
        # Surfaces are declared so schema validation still happens locally
        # before we forward (catches bad payloads at the source Core).
        surfaces: dict[str, dict] = {}
        for s in entry.get("surfaces", []) or []:
            schema_path = (manifest_dir / s["schema"]).resolve()
            schema = json.loads(schema_path.read_text())
            surfaces[s["name"]] = {
                "type": s["type"],
                "schema": schema,
                "invocation_mode": s.get("invocation_mode", "request_response"),
            }
        # Stub decl. `secret` is set to a sentinel so any accidental verify()
        # against this node fails closed.
        state.nodes_decl[nid] = {
            "kind": kind,
            "runtime": "remote-peer",
            "metadata": {"peer": peer, "remote": True},
            "secret": "__remote_no_local_verify__",
            "surfaces": surfaces,
        }
        fed.remote_nodes[nid] = RemoteNodeSpec(
            node_id=nid, peer=peer, kind=kind,
            surfaces=list(entry.get("surfaces", []) or []),
        )

    return fed


# ---------- peer envelope helpers ----------


def build_peer_envelope(*, peer_from: str, peer_to: str, inner: dict, secret: str) -> dict:
    """Wrap an inner envelope for transport between Cores.

    Schema:
      peer_from   — logical name of sending Core
      peer_to     — logical name of receiving Core
      nonce       — random per-message UUID; receiver de-dupes
      timestamp   — RFC3339; receiver enforces clock-skew window
      inner       — the original node-to-node envelope, untouched
      signature   — HMAC-SHA256 over canonical JSON of all other fields
    """
    env = {
        "peer_from": peer_from,
        "peer_to": peer_to,
        "nonce": uuid.uuid4().hex,
        "timestamp": now_iso(),
        "inner": inner,
    }
    env["signature"] = sign(env, secret)
    return env


def verify_peer_envelope(env: dict, secret: str) -> bool:
    return verify(env, secret)


def parse_iso(ts: str) -> float | None:
    """Parse RFC3339 timestamp. Returns POSIX seconds, or None on failure."""
    try:
        # _dt.datetime.fromisoformat handles 'Z' suffix only on Python 3.11+,
        # but core/.now_iso always emits +00:00, so we don't need a fallback.
        dt = _dt.datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _within_skew(ts: str, now: float, bound: int) -> bool:
    parsed = parse_iso(ts)
    if parsed is None:
        return False
    return abs(now - parsed) <= bound


# ---------- federated handlers ----------


async def federated_handle_invoke(request: web.Request) -> web.Response:
    """Replacement for Core's /v0/invoke: detects remote targets, forwards.

    Local invocations fall through to the unmodified `_route_invocation` from
    `core.core`. Remote invocations are verified locally, wrapped in a peer
    envelope, and forwarded to the peer Core. The peer Core's response is
    returned verbatim to the calling node.
    """
    state: CoreState = request.app["state"]
    fed: FederationState = state.federation  # type: ignore[attr-defined]
    env = await request.json()
    to = env.get("to") or ""
    target_node = to.split(".", 1)[0] if "." in to else None

    if target_node and target_node in fed.remote_nodes:
        return await _forward_invocation_to_peer(state, fed, env)

    status, body = await _route_invocation(state, env)
    return web.json_response(body, status=status)


async def _forward_invocation_to_peer(
    state: CoreState, fed: FederationState, env: dict
) -> web.Response:
    """Local pre-flight checks + peer-link POST. Returns whatever B returned."""
    from_node = env.get("from")
    decl = state.nodes_decl.get(from_node) if from_node else None
    if not decl:
        return web.json_response(
            {"error": "unknown_node", "from": from_node}, status=404
        )
    if decl.get("secret") == "__remote_no_local_verify__":
        # A remote node can't initiate from this Core: it lives on a peer.
        return web.json_response(
            {"error": "remote_node_cannot_originate", "from": from_node},
            status=400,
        )
    if not verify(env, decl["secret"]):
        await state.audit(
            type="invocation",
            from_node=from_node,
            to_surface=env.get("to"),
            decision="denied_signature_invalid",
            correlation_id=env.get("correlation_id") or env.get("id"),
            details={"phase": "peer_forward"},
        )
        return web.json_response({"error": "bad_signature"}, status=401)

    target_node, _, surface_name = env["to"].partition(".")
    remote = fed.remote_nodes[target_node]
    if surface_name not in {s["name"] for s in remote.surfaces}:
        return web.json_response({"error": "unknown_surface"}, status=404)

    # Edge-check at originating Core too — a node should not be able to
    # silently invoke remote surfaces it has no relationship to.
    if (from_node, env["to"]) not in state.edges:
        return web.json_response(
            {"error": "denied_no_relationship", "from": from_node, "to": env["to"]},
            status=403,
        )

    peer = fed.peers[remote.peer]
    wrapped = build_peer_envelope(
        peer_from=fed.local_name, peer_to=peer.name, inner=env, secret=peer.secret
    )

    state.emit_envelope(
        env=env, direction="out", signature_valid=True, route_status="forwarded_peer",
    )
    await state.audit(
        type="peer_forward",
        from_node=from_node,
        to_surface=env["to"],
        decision="forwarded",
        correlation_id=env.get("correlation_id") or env.get("id"),
        details={"peer": peer.name, "nonce": wrapped["nonce"]},
    )

    try:
        http = await fed.http()
        timeout = aiohttp.ClientTimeout(total=PEER_FORWARD_TIMEOUT)
        url = f"{peer.url}/v0/peer/envelope"
        async with http.post(url, json=wrapped, timeout=timeout) as r:
            data = await r.json()
            return web.json_response(data, status=r.status)
    except aiohttp.ClientError as e:
        await state.audit(
            type="peer_forward",
            from_node=from_node,
            to_surface=env["to"],
            decision="peer_unreachable",
            correlation_id=env.get("correlation_id") or env.get("id"),
            details={"peer": peer.name, "error": str(e)},
        )
        return web.json_response(
            {"error": "peer_unreachable", "peer": peer.name, "details": str(e)},
            status=502,
        )
    except asyncio.TimeoutError:
        return web.json_response(
            {"error": "peer_timeout", "peer": peer.name}, status=504
        )


async def handle_peer_envelope(request: web.Request) -> web.Response:
    """Receiving side: verify peer signature + replay protection, then route.

    The inner envelope is dispatched through the unmodified `_route_invocation`
    with `signature_pre_verified=True`. The chain is:
      * inner.from is a remote-node stub on this Core; its `secret` is the
        sentinel (`__remote_no_local_verify__`), so direct verify() would
        fail, but the pre-verified flag short-circuits the check.
      * the (from, to) edge must still be declared in this Core's manifest.
      * the inner payload must still match the surface schema.
    """
    state: CoreState = request.app["state"]
    fed: FederationState = state.federation  # type: ignore[attr-defined]
    try:
        peer_env = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "bad_json"}, status=400)

    peer_from = peer_env.get("peer_from")
    peer_to = peer_env.get("peer_to")
    nonce = peer_env.get("nonce")
    ts = peer_env.get("timestamp")
    inner = peer_env.get("inner")

    if not all(isinstance(x, str) and x for x in (peer_from, peer_to, nonce, ts)):
        return web.json_response({"error": "missing_peer_fields"}, status=400)
    if not isinstance(inner, dict):
        return web.json_response({"error": "missing_inner"}, status=400)

    peer = fed.peers.get(peer_from)
    if peer is None:
        return web.json_response(
            {"error": "unknown_peer", "peer_from": peer_from}, status=403
        )

    if peer_to != fed.local_name:
        # Misrouted. Don't process; an attacker might be testing whether
        # different local names yield different errors.
        return web.json_response(
            {"error": "wrong_peer_to", "expected": fed.local_name, "got": peer_to},
            status=400,
        )

    if not verify_peer_envelope(peer_env, peer.secret):
        await state.audit(
            type="peer_inbound",
            decision="denied_peer_signature",
            details={"peer_from": peer_from, "nonce": nonce},
        )
        return web.json_response({"error": "bad_peer_signature"}, status=401)

    now = time.time()
    if not _within_skew(ts, now, PEER_SKEW_SECONDS):
        return web.json_response(
            {"error": "peer_skew", "skew_bound_seconds": PEER_SKEW_SECONDS},
            status=400,
        )

    inner_ts = inner.get("timestamp")
    if isinstance(inner_ts, str):
        if not _within_skew(inner_ts, now, PEER_SKEW_SECONDS):
            return web.json_response(
                {"error": "inner_skew", "skew_bound_seconds": PEER_SKEW_SECONDS},
                status=400,
            )

    fresh = await fed.remember_nonce(peer_from, nonce)
    if not fresh:
        await state.audit(
            type="peer_inbound",
            decision="denied_replay",
            details={"peer_from": peer_from, "nonce": nonce},
        )
        return web.json_response({"error": "peer_replay"}, status=409)

    inner_from = inner.get("from")
    inner_decl = state.nodes_decl.get(inner_from) if inner_from else None
    if not inner_decl:
        return web.json_response(
            {"error": "unknown_inner_from", "from": inner_from}, status=404
        )
    # Sanity: inner.from must be a remote-node stub registered for THIS peer.
    expected_remote = fed.remote_nodes.get(inner_from)
    if expected_remote is None or expected_remote.peer != peer_from:
        return web.json_response(
            {"error": "inner_from_not_owned_by_peer",
             "from": inner_from, "peer_from": peer_from},
            status=403,
        )

    await state.audit(
        type="peer_inbound",
        from_node=inner_from,
        to_surface=inner.get("to"),
        decision="accepted",
        correlation_id=inner.get("correlation_id") or inner.get("id"),
        details={"peer_from": peer_from, "nonce": nonce},
    )
    state.emit_envelope(
        env=inner, direction="in", signature_valid=True, route_status="peer_accepted"
    )

    status, body = await _route_invocation(state, inner, signature_pre_verified=True)
    return web.json_response(body, status=status)


# ---------- federated /v0/respond ----------
#
# Responses today are 1:1 with /v0/invoke: the Core that handled the
# invocation also matches the response future. In the federation flow,
# Core B awaits beta's response inside _route_invocation and returns the
# response body as the HTTP body of /v0/peer/envelope. Core A receives it
# as the HTTP body of /v0/invoke. So nothing extra is needed for /v0/respond
# in the federated flow — Core's stock handler is reused.


# ---------- bootstrap ----------


def _register_routes(app: web.Application) -> None:
    """Mirror core.core.make_app's route table, swapping /v0/invoke."""
    app.router.add_post("/v0/register", handle_register)
    app.router.add_post("/v0/invoke", federated_handle_invoke)
    app.router.add_post("/v0/respond", handle_respond)
    app.router.add_get("/v0/stream", handle_stream)
    app.router.add_get("/v0/healthz", handle_health)
    app.router.add_get("/v0/introspect", handle_introspect)
    app.router.add_get("/v0/admin/state", handle_admin_state)
    app.router.add_get("/v0/admin/stream", handle_admin_stream)
    app.router.add_post("/v0/admin/manifest", handle_admin_manifest)
    app.router.add_post("/v0/admin/reload", handle_admin_reload)
    app.router.add_post("/v0/admin/invoke", handle_admin_invoke)
    app.router.add_post("/v0/admin/node_status", handle_admin_node_status)
    app.router.add_get("/v0/admin/ui_state", handle_admin_ui_state)
    app.router.add_get("/v0/admin/processes", handle_admin_processes)
    app.router.add_post("/v0/admin/spawn", handle_admin_spawn)
    app.router.add_post("/v0/admin/stop", handle_admin_stop)
    app.router.add_post("/v0/admin/restart", handle_admin_restart)
    app.router.add_post("/v0/admin/reconcile", handle_admin_reconcile)
    # Federation surfaces.
    app.router.add_post("/v0/peer/envelope", handle_peer_envelope)
    app.router.add_get("/v0/peer/info", handle_peer_info)


async def handle_peer_info(request: web.Request) -> web.Response:
    """Diagnostic: who am I, who are my peers, what remote nodes do I serve?"""
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    fed: FederationState = state.federation  # type: ignore[attr-defined]
    return web.json_response({
        "local_name": fed.local_name,
        "peers": [
            {"name": p.name, "url": p.url} for p in fed.peers.values()
        ],
        "remote_nodes": [
            {"id": rn.id, "peer": rn.peer, "kind": rn.kind,
             "surfaces": [s["name"] for s in rn.surfaces]}
            for rn in fed.remote_nodes.values()
        ],
    })


def make_federated_app(
    manifest_path: str, audit_path: str | None = None
) -> web.Application:
    audit_path = audit_path or os.environ.get("AUDIT_LOG", "audit.log")
    app = web.Application(client_max_size=10 * 1024 * 1024, middlewares=[_cors_middleware])
    state = CoreState(manifest_path, audit_path)
    state.load_manifest()
    fed = load_federation(manifest_path, state)
    state.federation = fed  # type: ignore[attr-defined]
    app["state"] = state
    _register_routes(app)

    async def on_shutdown(_app: web.Application) -> None:
        await fed.close()
        for q in list(state._streams):  # noqa: SLF001
            try:
                q.put_nowait({"type": "_close", "data": {}})
            except asyncio.QueueFull:
                pass

    app.on_shutdown.append(on_shutdown)
    return app
