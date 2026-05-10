"""RAVEN Mesh — single-process Python Core.

Implements the v0 wire protocol (PRD §5). Loads a manifest, listens on HTTP,
verifies HMAC signatures, validates payloads against per-surface JSON Schemas,
and routes messages between connected nodes via SSE delivery + POST responses.
Audit log is JSON-per-line.

Admin surfaces (see PRD §8):
    GET  /v0/admin/state         — full snapshot of nodes, manifest, edges, tail
    GET  /v0/admin/stream        — SSE tap of every envelope flowing through Core
    POST /v0/admin/manifest      — write+validate a new manifest YAML to disk
    POST /v0/admin/reload        — re-read the manifest currently on disk
    POST /v0/admin/invoke        — synthesize a signed envelope from a chosen node
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as _dt
import hashlib
import hmac
import json
import os
import pathlib
import signal
import sys
import time
import uuid
from typing import Any

import yaml
from aiohttp import web
from jsonschema import ValidationError, validate as jsonschema_validate

from core.supervisor import Supervisor, make_script_resolver


ENVELOPE_TAIL_MAX = 200
NODE_QUEUE_MAX = 1024
LEGACY_ADMIN_TOKEN = "admin-dev-token"
ADMIN_RATE_BUCKET_MAX = 4096


# ---------- helpers ----------

def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def canonical(obj: dict) -> str:
    body = {k: v for k, v in obj.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def sign(obj: dict, secret: str) -> str:
    return hmac.new(secret.encode(), canonical(obj).encode(), hashlib.sha256).hexdigest()


def verify(obj: dict, secret: str) -> bool:
    sig = obj.get("signature")
    if not isinstance(sig, str):
        return False
    return hmac.compare_digest(sig, sign(obj, secret))


def admin_token() -> str:
    """Return the configured admin token, refusing unset/legacy defaults.

    Raises ``RuntimeError`` if ``ADMIN_TOKEN`` is missing or equals the
    historical ``"admin-dev-token"`` placeholder. Failing loud at boot beats
    silently shipping a guessable fallback.
    """
    tok = os.environ.get("ADMIN_TOKEN")
    if not tok:
        raise RuntimeError(
            "ADMIN_TOKEN must be set in the environment before Core starts; "
            "there is no built-in default."
        )
    if tok == LEGACY_ADMIN_TOKEN:
        raise RuntimeError(
            "ADMIN_TOKEN is set to the legacy placeholder 'admin-dev-token'; "
            "rotate to a non-default value before starting Core."
        )
    return tok


def _admin_authed(request: web.Request) -> bool:
    # Header-only. Query-string secrets land in shell history, browser
    # history, server logs and Referer headers — refuse them outright.
    token = request.headers.get("X-Admin-Token")
    if not token:
        return False
    try:
        expected = admin_token()
    except RuntimeError:
        return False
    return hmac.compare_digest(token, expected)


# ---------- state ----------

class CoreState:
    def __init__(self, manifest_path: str, audit_path: str):
        self.manifest_path = pathlib.Path(manifest_path).resolve()
        self.audit_path = pathlib.Path(audit_path).resolve()
        self.nodes_decl: dict[str, dict] = {}
        self.connections: dict[str, dict] = {}
        self.sessions: dict[str, str] = {}
        self.edges: set[tuple[str, str]] = set()
        self.pending: dict[str, dict] = {}
        self.audit_lock = asyncio.Lock()
        self._streams: set[asyncio.Queue] = set()
        # Admin tap.
        self._admin_streams: set[asyncio.Queue] = set()
        self.envelope_tail: collections.deque = collections.deque(maxlen=ENVELOPE_TAIL_MAX)
        # Process supervisor (set by make_app after manifest loads).
        # Optional — Core works fine without it; falls back to scripts/run_mesh.sh
        # owning processes. When attached, /v0/admin/{spawn,stop,restart,reconcile}
        # endpoints become functional.
        self.supervisor: Supervisor | None = None
        # Raw manifest nodes keyed by id (for supervisor reconcile + spawn args).
        self.manifest_nodes_raw: dict[str, dict] = {}

    # manifest -----------------------------------------------------------

    def load_manifest(self) -> None:
        self._reset_manifest_state()
        text = self.manifest_path.read_text()
        m = yaml.safe_load(text)
        manifest_dir = self.manifest_path.parent
        for node in m.get("nodes", []):
            secret = self._resolve_secret(node["id"], node.get("identity_secret", ""))
            surfaces: dict[str, dict] = {}
            for s in node.get("surfaces", []):
                schema_path = (manifest_dir / s["schema"]).resolve()
                schema = json.loads(schema_path.read_text())
                surfaces[s["name"]] = {
                    "type": s["type"],
                    "schema": schema,
                    "invocation_mode": s.get("invocation_mode", "request_response"),
                }
            self.nodes_decl[node["id"]] = {
                "kind": node["kind"],
                "runtime": node.get("runtime", "local-process"),
                "metadata": node.get("metadata", {}),
                "secret": secret,
                "surfaces": surfaces,
            }
            # Keep the raw node dict for supervisor consumption.
            self.manifest_nodes_raw[node["id"]] = node
        for rel in m.get("relationships", []):
            self.edges.add((rel["from"], rel["to"]))

    def _reset_manifest_state(self) -> None:
        # Connections, sessions, pending, tail, streams stay live across reload.
        self.nodes_decl = {}
        self.edges = set()
        self.manifest_nodes_raw = {}

    def _resolve_secret(self, node_id: str, spec: str) -> str:
        if spec.startswith("env:"):
            var = spec[4:]
            val = os.environ.get(var)
            if val:
                return val
            val = hashlib.sha256(f"mesh:{node_id}:autogen".encode()).hexdigest()
            os.environ[var] = val
            return val
        return spec or hashlib.sha256(f"mesh:{node_id}:autogen".encode()).hexdigest()

    def relationships_for(self, node_id: str) -> list[dict]:
        out = []
        for f, t in sorted(self.edges):
            if f == node_id or t.split(".", 1)[0] == node_id:
                out.append({"from": f, "to": t})
        return out

    # audit + tap --------------------------------------------------------

    async def audit(self, **fields: Any) -> None:
        evt = {"id": str(uuid.uuid4()), "timestamp": now_iso(), **fields}
        line = json.dumps(evt) + "\n"
        async with self.audit_lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a") as f:
                f.write(line)

    def emit_envelope(self, *, env: dict, direction: str,
                      signature_valid: bool, route_status: str) -> None:
        evt = {
            "ts": now_iso(),
            "direction": direction,
            "from_node": env.get("from"),
            "to_surface": env.get("to"),
            "msg_id": env.get("id"),
            "correlation_id": env.get("correlation_id"),
            "kind": env.get("kind"),
            "payload": env.get("payload", {}),
            "wrapped": env.get("wrapped"),
            "signature_valid": signature_valid,
            "route_status": route_status,
        }
        self.envelope_tail.append(evt)
        if not self._admin_streams:
            return
        for q in list(self._admin_streams):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass


# ---------- handlers ----------

async def handle_register(request: web.Request) -> web.Response:
    state: CoreState = request.app["state"]
    body = await request.json()
    node_id = body.get("node_id")
    decl = state.nodes_decl.get(node_id) if node_id else None
    if not decl:
        return web.json_response({"error": "unknown_node", "node_id": node_id}, status=404)
    if not verify(body, decl["secret"]):
        return web.json_response({"error": "bad_signature"}, status=401)
    old = state.connections.get(node_id)
    if old:
        state.sessions.pop(old["session_id"], None)
        try:
            old["queue"].put_nowait({"type": "_close", "data": {}})
        except asyncio.QueueFull:
            pass
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue(maxsize=NODE_QUEUE_MAX)
    state.connections[node_id] = {
        "session_id": session_id,
        "queue": queue,
        "connected_at": now_iso(),
    }
    state.sessions[session_id] = node_id
    surfaces_view = []
    for name, s in decl["surfaces"].items():
        surfaces_view.append({
            "name": name,
            "type": s["type"],
            "invocation_mode": s["invocation_mode"],
        })
    return web.json_response({
        "session_id": session_id,
        "node_id": node_id,
        "kind": decl["kind"],
        "surfaces": surfaces_view,
        "relationships": state.relationships_for(node_id),
    })


async def _route_invocation(state: CoreState, env: dict,
                             *, signature_pre_verified: bool = False) -> tuple[int, dict]:
    """Core invocation routing. Returns (http_status, response_dict).

    Used by both /v0/invoke (signature verified inside) and /v0/admin/invoke
    (signature synthesized by Core, so verification is skipped).
    """
    msg_id = env.get("id") or str(uuid.uuid4())
    env.setdefault("id", msg_id)
    correlation_id = env.get("correlation_id") or msg_id
    env.setdefault("correlation_id", correlation_id)
    from_node = env.get("from")
    to = env.get("to")
    if env.get("kind") not in (None, "invocation"):
        state.emit_envelope(env=env, direction="in", signature_valid=signature_pre_verified,
                            route_status="bad_kind")
        return 400, {"error": "bad_kind", "expected": "invocation"}
    decl = state.nodes_decl.get(from_node) if from_node else None
    if not decl:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_unknown_node", correlation_id=correlation_id, details={})
        state.emit_envelope(env=env, direction="in", signature_valid=False,
                            route_status="denied_unknown_node")
        return 404, {"error": "unknown_node"}
    sig_valid = signature_pre_verified or verify(env, decl["secret"])
    if not sig_valid:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_signature_invalid", correlation_id=correlation_id, details={})
        state.emit_envelope(env=env, direction="in", signature_valid=False,
                            route_status="denied_signature_invalid")
        return 401, {"error": "bad_signature"}
    if not isinstance(to, str) or "." not in to:
        state.emit_envelope(env=env, direction="in", signature_valid=True,
                            route_status="bad_surface_id")
        return 400, {"error": "bad_surface_id"}
    if (from_node, to) not in state.edges:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_no_relationship", correlation_id=correlation_id, details={})
        state.emit_envelope(env=env, direction="in", signature_valid=True,
                            route_status="denied_no_relationship")
        return 403, {"error": "denied_no_relationship", "from": from_node, "to": to}
    target_node, surface_name = to.split(".", 1)
    target_decl = state.nodes_decl.get(target_node)
    if not target_decl or surface_name not in target_decl["surfaces"]:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_unknown_surface", correlation_id=correlation_id, details={})
        state.emit_envelope(env=env, direction="in", signature_valid=True,
                            route_status="denied_unknown_surface")
        return 404, {"error": "unknown_surface"}
    surface = target_decl["surfaces"][surface_name]
    try:
        jsonschema_validate(env.get("payload", {}), surface["schema"])
    except ValidationError as e:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_schema_invalid", correlation_id=correlation_id,
                          details={"error": str(e)[:500]})
        state.emit_envelope(env=env, direction="in", signature_valid=True,
                            route_status="denied_schema_invalid")
        return 400, {"error": "denied_schema_invalid", "details": str(e)}
    target_conn = state.connections.get(target_node)
    if not target_conn:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_node_unreachable", correlation_id=correlation_id, details={})
        state.emit_envelope(env=env, direction="in", signature_valid=True,
                            route_status="denied_node_unreachable")
        return 503, {"error": "denied_node_unreachable", "node": target_node}
    deliver_event = {"type": "deliver", "data": env}
    target_queue: asyncio.Queue = target_conn["queue"]
    if surface["invocation_mode"] == "fire_and_forget":
        try:
            target_queue.put_nowait(deliver_event)
        except asyncio.QueueFull:
            await state.audit(type="invocation", from_node=from_node, to_surface=to,
                              decision="denied_queue_full", correlation_id=correlation_id,
                              details={"target_node": target_node, "queue_max": NODE_QUEUE_MAX})
            state.emit_envelope(env=env, direction="in", signature_valid=True,
                                route_status="denied_queue_full")
            return 503, {"error": "denied_queue_full", "node": target_node}
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="routed", correlation_id=correlation_id, details={"msg_id": msg_id})
        state.emit_envelope(env=env, direction="in", signature_valid=True, route_status="routed")
        return 202, {"id": msg_id, "status": "accepted"}
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending[msg_id] = {"future": fut, "target_node": target_node, "from_node": from_node}
    try:
        target_queue.put_nowait(deliver_event)
    except asyncio.QueueFull:
        state.pending.pop(msg_id, None)
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_queue_full", correlation_id=correlation_id,
                          details={"target_node": target_node, "queue_max": NODE_QUEUE_MAX})
        state.emit_envelope(env=env, direction="in", signature_valid=True,
                            route_status="denied_queue_full")
        return 503, {"error": "denied_queue_full", "node": target_node}
    await state.audit(type="invocation", from_node=from_node, to_surface=to,
                      decision="routed", correlation_id=correlation_id, details={"msg_id": msg_id})
    state.emit_envelope(env=env, direction="in", signature_valid=True, route_status="routed")
    timeout = float(os.environ.get("MESH_INVOKE_TIMEOUT", "30"))
    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        state.pending.pop(msg_id, None)
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="timeout", correlation_id=correlation_id, details={})
        return 504, {"error": "timeout", "id": msg_id}
    finally:
        state.pending.pop(msg_id, None)
    return 200, result


async def handle_invoke(request: web.Request) -> web.Response:
    state: CoreState = request.app["state"]
    env = await request.json()
    status, body = await _route_invocation(state, env)
    return web.json_response(body, status=status)


async def handle_respond(request: web.Request) -> web.Response:
    state: CoreState = request.app["state"]
    env = await request.json()
    from_node = env.get("from")
    decl = state.nodes_decl.get(from_node) if from_node else None
    if not decl:
        return web.json_response({"error": "unknown_node"}, status=404)
    if not verify(env, decl["secret"]):
        return web.json_response({"error": "bad_signature"}, status=401)
    if env.get("kind") not in ("response", "error"):
        return web.json_response({"error": "bad_kind", "expected": "response|error"}, status=400)
    correlation_id = env.get("correlation_id")
    if not correlation_id:
        return web.json_response({"error": "missing_correlation_id"}, status=400)
    entry = state.pending.get(correlation_id)
    if not entry or entry["future"].done():
        return web.json_response({"error": "no_pending_request", "correlation_id": correlation_id}, status=404)
    if entry["target_node"] != from_node:
        return web.json_response({"error": "responder_not_target", "expected": entry["target_node"]}, status=403)
    await state.audit(type="response", from_node=from_node, to_surface=env.get("to", ""),
                      decision="routed", correlation_id=correlation_id,
                      details={"kind": env.get("kind")})
    state.emit_envelope(env=env, direction="out", signature_valid=True, route_status="routed")
    entry["future"].set_result(env)
    return web.json_response({"status": "accepted"}, status=200)


async def handle_stream(request: web.Request) -> web.StreamResponse:
    state: CoreState = request.app["state"]
    session = request.query.get("session")
    node_id = state.sessions.get(session) if session else None
    if not node_id:
        return web.json_response({"error": "unknown_session"}, status=401)
    conn = state.connections.get(node_id)
    if not conn or conn["session_id"] != session:
        return web.json_response({"error": "stale_session"}, status=401)
    response = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)
    queue: asyncio.Queue = conn["queue"]
    request.app["state"]._streams.add(queue)  # noqa: SLF001
    try:
        await response.write(
            f"event: hello\ndata: {json.dumps({'node_id': node_id, 'session_id': session})}\n\n".encode()
        )
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    await response.write(b": heartbeat\n\n")
                except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                    break
                continue
            if event.get("type") == "_close":
                break
            line = f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
            try:
                await response.write(line.encode())
            except (ConnectionResetError, BrokenPipeError):
                await queue.put(event)
                break
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        request.app["state"]._streams.discard(queue)  # noqa: SLF001
    return response


async def handle_health(request: web.Request) -> web.Response:
    state: CoreState = request.app["state"]
    return web.json_response({
        "ok": True,
        "nodes_declared": len(state.nodes_decl),
        "nodes_connected": len(state.connections),
        "edges": len(state.edges),
        "pending": len(state.pending),
    })


async def handle_introspect(request: web.Request) -> web.Response:
    state: CoreState = request.app["state"]
    nodes = []
    for nid, decl in state.nodes_decl.items():
        nodes.append({
            "id": nid,
            "kind": decl["kind"],
            "runtime": decl["runtime"],
            "metadata": decl["metadata"],
            "connected": nid in state.connections,
            "surfaces": [
                {"name": n, "type": s["type"], "invocation_mode": s["invocation_mode"]}
                for n, s in decl["surfaces"].items()
            ],
        })
    edges = [{"from": f, "to": t} for f, t in sorted(state.edges)]
    return web.json_response({"nodes": nodes, "relationships": edges})


# ---------- admin ----------

def _nodes_state_view(state: CoreState) -> list[dict]:
    out = []
    for nid, decl in state.nodes_decl.items():
        out.append({
            "id": nid,
            "kind": decl["kind"],
            "runtime": decl["runtime"],
            "metadata": decl["metadata"],
            "connected": nid in state.connections,
            "surfaces": [
                {
                    "name": n,
                    "type": s["type"],
                    "invocation_mode": s["invocation_mode"],
                    "schema": s["schema"],
                }
                for n, s in decl["surfaces"].items()
            ],
        })
    return out


async def handle_admin_state(request: web.Request) -> web.Response:
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    return web.json_response({
        "manifest_path": str(state.manifest_path),
        "audit_path": str(state.audit_path),
        "nodes": _nodes_state_view(state),
        "relationships": [{"from": f, "to": t} for f, t in sorted(state.edges)],
        "envelope_tail": list(state.envelope_tail),
    })


async def handle_admin_stream(request: web.Request) -> web.StreamResponse:
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    response = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)
    queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
    state._admin_streams.add(queue)  # noqa: SLF001
    try:
        await response.write(b"event: hello\ndata: {}\n\n")
        for evt in list(state.envelope_tail):
            await response.write(
                f"event: envelope\ndata: {json.dumps(evt)}\n\n".encode()
            )
        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    await response.write(b": heartbeat\n\n")
                except (ConnectionResetError, BrokenPipeError):
                    break
                continue
            try:
                await response.write(
                    f"event: envelope\ndata: {json.dumps(evt)}\n\n".encode()
                )
            except (ConnectionResetError, BrokenPipeError):
                break
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        state._admin_streams.discard(queue)  # noqa: SLF001
    return response


async def handle_admin_manifest(request: web.Request) -> web.Response:
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    raw = await request.text()
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return web.json_response({"error": "bad_yaml", "details": str(e)}, status=400)
    if not isinstance(parsed, dict) or "nodes" not in parsed:
        return web.json_response({"error": "manifest_missing_nodes"}, status=400)
    backup = state.manifest_path.with_suffix(state.manifest_path.suffix + ".bak")
    if state.manifest_path.exists():
        backup.write_text(state.manifest_path.read_text())
    state.manifest_path.write_text(raw)
    try:
        state.load_manifest()
    except Exception as e:
        if backup.exists():
            state.manifest_path.write_text(backup.read_text())
            state.load_manifest()
        return web.json_response({"error": "load_failed", "details": str(e)}, status=400)
    return web.json_response({
        "ok": True,
        "manifest_path": str(state.manifest_path),
        "nodes_declared": len(state.nodes_decl),
        "edges": len(state.edges),
    })


async def handle_admin_reload(request: web.Request) -> web.Response:
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    try:
        state.load_manifest()
    except Exception as e:
        return web.json_response({"error": "load_failed", "details": str(e)}, status=400)
    return web.json_response({
        "ok": True,
        "nodes_declared": len(state.nodes_decl),
        "edges": len(state.edges),
    })


# ---------- supervisor admin endpoints ----------
#
# These four endpoints are no-ops if the Core was started without
# --supervisor / MESH_SUPERVISOR=1. In that mode, scripts/run_mesh.sh
# still owns process lifecycle. With supervisor enabled, Core owns it.

def _require_supervisor(state: CoreState) -> web.Response | None:
    if state.supervisor is None:
        return web.json_response(
            {"error": "supervisor_disabled",
             "details": "Core started without --supervisor; "
                        "scripts/run_mesh.sh owns processes"},
            status=409,
        )
    return None


async def handle_admin_processes(request: web.Request) -> web.Response:
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    if state.supervisor is None:
        return web.json_response({"supervisor_enabled": False, "processes": []})
    return web.json_response({
        "supervisor_enabled": True,
        "processes": state.supervisor.list_processes(),
    })


async def handle_admin_spawn(request: web.Request) -> web.Response:
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    err = _require_supervisor(state)
    if err is not None:
        return err
    body = await request.json()
    nid = body.get("node_id")
    if not nid:
        return web.json_response({"error": "missing_node_id"}, status=400)
    manifest_node = state.manifest_nodes_raw.get(nid)
    if manifest_node is None:
        return web.json_response({"error": "unknown_node",
                                  "details": f"{nid} not in current manifest"},
                                 status=404)
    res = await state.supervisor.spawn(nid, manifest_node)
    return web.json_response(res)


async def handle_admin_stop(request: web.Request) -> web.Response:
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    err = _require_supervisor(state)
    if err is not None:
        return err
    body = await request.json()
    nid = body.get("node_id")
    graceful = bool(body.get("graceful", True))
    if not nid:
        return web.json_response({"error": "missing_node_id"}, status=400)
    res = await state.supervisor.stop(nid, graceful=graceful)
    return web.json_response(res)


async def handle_admin_restart(request: web.Request) -> web.Response:
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    err = _require_supervisor(state)
    if err is not None:
        return err
    body = await request.json()
    nid = body.get("node_id")
    if not nid:
        return web.json_response({"error": "missing_node_id"}, status=400)
    manifest_node = state.manifest_nodes_raw.get(nid)
    if manifest_node is None:
        return web.json_response({"error": "unknown_node"}, status=404)
    res = await state.supervisor.restart(nid, manifest_node)
    return web.json_response(res)


async def handle_admin_reconcile(request: web.Request) -> web.Response:
    """Diff manifest desired set vs. running set. Spawn missing, stop extras."""
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    err = _require_supervisor(state)
    if err is not None:
        return err
    res = await state.supervisor.reconcile(state.manifest_nodes_raw)
    return web.json_response(res)


async def handle_admin_drain(request: web.Request) -> web.Response:
    """Stop accepting new work for a child, wait for in-flight to finish, then SIGTERM."""
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    err = _require_supervisor(state)
    if err is not None:
        return err
    body = await request.json()
    nid = body.get("node_id")
    timeout = float(body.get("timeout", 30.0))
    if not nid:
        return web.json_response({"error": "missing_node_id"}, status=400)
    res = await state.supervisor.drain(nid, timeout=timeout)
    return web.json_response(res)


async def handle_admin_metrics(request: web.Request) -> web.Response:
    """Per-child + aggregate supervisor metrics (restart counts, uptime, in-flight)."""
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    if state.supervisor is None:
        return web.json_response({"supervisor_enabled": False, "metrics": None})
    return web.json_response({
        "supervisor_enabled": True,
        "metrics": state.supervisor.metrics(),
    })


async def handle_admin_invoke(request: web.Request) -> web.Response:
    """Synthesize a signed envelope from a chosen registered node and route it."""
    if not _admin_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    state: CoreState = request.app["state"]
    body = await request.json()
    from_node = body.get("from_node")
    target = body.get("target")
    payload = body.get("payload", {})
    decl = state.nodes_decl.get(from_node) if from_node else None
    if not decl:
        return web.json_response({"error": "unknown_node", "from_node": from_node}, status=404)
    if not isinstance(target, str) or "." not in target:
        return web.json_response({"error": "bad_target"}, status=400)
    msg_id = str(uuid.uuid4())
    env = {
        "id": msg_id,
        "correlation_id": msg_id,
        "from": from_node,
        "to": target,
        "kind": "invocation",
        "payload": payload,
        "timestamp": now_iso(),
    }
    env["signature"] = sign(env, decl["secret"])
    status, result = await _route_invocation(state, env, signature_pre_verified=True)
    return web.json_response(result, status=status)


@web.middleware
async def _cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, X-Admin-Token",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        })
    response = await handler(request)
    if request.path.startswith("/v0/admin"):
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token")
    return response


class _AdminRateLimiter:
    """Token-bucket rate limiter scoped to ``/v0/admin/*``.

    Generic protocol-layer protection: any caller of the admin namespace —
    operator script, dashboard, malicious neighbour — sees the same bucket.
    Configured via ``MESH_ADMIN_RATE_LIMIT`` (per-minute fill rate,
    default 60) and ``MESH_ADMIN_RATE_BURST`` (bucket capacity / burst
    allowance, default 20). Setting the rate to ``0`` disables limiting.
    """

    def __init__(self, rate_per_min: float, burst: float):
        self.refill_per_sec = rate_per_min / 60.0 if rate_per_min > 0 else 0.0
        self.capacity = float(burst) if burst > 0 else 0.0
        self.enabled = self.refill_per_sec > 0 and self.capacity > 0
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def consume(self, key: str) -> bool:
        if not self.enabled:
            return True
        now = time.monotonic()
        async with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_sec)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            tokens -= 1.0
            if len(self._buckets) > ADMIN_RATE_BUCKET_MAX:
                self._evict_idle(now)
            self._buckets[key] = (tokens, now)
            return True

    def _evict_idle(self, now: float) -> None:
        # Drop entries that have refilled to capacity — equivalent to "no
        # state worth keeping". Keeps the dict bounded even under spray.
        idle_after = self.capacity / self.refill_per_sec if self.refill_per_sec else 0
        cutoff = now - idle_after
        for k in [k for k, (_, last) in self._buckets.items() if last < cutoff]:
            self._buckets.pop(k, None)


def _build_admin_rate_limiter() -> _AdminRateLimiter:
    try:
        rate = float(os.environ.get("MESH_ADMIN_RATE_LIMIT", "60"))
    except ValueError:
        rate = 60.0
    try:
        burst = float(os.environ.get("MESH_ADMIN_RATE_BURST", "20"))
    except ValueError:
        burst = 20.0
    return _AdminRateLimiter(rate, burst)


def _admin_rate_key(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",", 1)[0].strip()
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if peer:
        return peer[0]
    return "unknown"


@web.middleware
async def _admin_rate_limit_middleware(request: web.Request, handler):
    if not request.path.startswith("/v0/admin"):
        return await handler(request)
    if request.method == "OPTIONS":
        return await handler(request)
    limiter: _AdminRateLimiter | None = request.app.get("admin_rate_limiter")
    if limiter is None or not limiter.enabled:
        return await handler(request)
    key = _admin_rate_key(request)
    if not await limiter.consume(key):
        return web.json_response(
            {"error": "rate_limited", "scope": "admin"},
            status=429,
            headers={"Retry-After": "1"},
        )
    return await handler(request)


# ---------- bootstrap ----------

def make_app(
    manifest_path: str,
    audit_path: str | None = None,
    *,
    enable_supervisor: bool = False,
    supervisor_log_dir: str = ".logs",
) -> web.Application:
    audit_path = audit_path or os.environ.get("AUDIT_LOG", "audit.log")
    # Validate the admin token at boot — refusing to start with an unset or
    # legacy-default token. The token is then resolved per-request.
    admin_token()
    app = web.Application(
        client_max_size=10 * 1024 * 1024,
        middlewares=[_cors_middleware, _admin_rate_limit_middleware],
    )
    app["admin_rate_limiter"] = _build_admin_rate_limiter()
    state = CoreState(manifest_path, audit_path)
    state.load_manifest()
    if enable_supervisor:
        repo_root = state.manifest_path.parent.parent  # manifests/foo.yaml -> repo
        # If manifest is at repo/manifests/x.yaml, repo_root is correct.
        # If manifest is somewhere weirder, fall back to cwd.
        if not (repo_root / "scripts").exists():
            repo_root = pathlib.Path.cwd()
        resolver = make_script_resolver(str(repo_root), supervisor_log_dir)

        async def emit_supervisor_event(evt: dict) -> None:
            # Pipe into the admin SSE tail so the dashboard sees live process events.
            payload = {"type": "supervisor", "data": evt}
            for q in list(state._admin_streams):  # noqa: SLF001
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

        state.supervisor = Supervisor(
            runner_resolver=resolver,
            log_dir=supervisor_log_dir,
            on_event=emit_supervisor_event,
        )
    app["state"] = state
    app.router.add_post("/v0/register", handle_register)
    app.router.add_post("/v0/invoke", handle_invoke)
    app.router.add_post("/v0/respond", handle_respond)
    app.router.add_get("/v0/stream", handle_stream)
    app.router.add_get("/v0/healthz", handle_health)
    app.router.add_get("/v0/introspect", handle_introspect)
    app.router.add_get("/v0/admin/state", handle_admin_state)
    app.router.add_get("/v0/admin/stream", handle_admin_stream)
    app.router.add_post("/v0/admin/manifest", handle_admin_manifest)
    app.router.add_post("/v0/admin/reload", handle_admin_reload)
    app.router.add_post("/v0/admin/invoke", handle_admin_invoke)
    # Supervisor endpoints (always registered; no-op if supervisor disabled).
    app.router.add_get("/v0/admin/processes", handle_admin_processes)
    app.router.add_post("/v0/admin/spawn", handle_admin_spawn)
    app.router.add_post("/v0/admin/stop", handle_admin_stop)
    app.router.add_post("/v0/admin/restart", handle_admin_restart)
    app.router.add_post("/v0/admin/reconcile", handle_admin_reconcile)
    app.router.add_post("/v0/admin/drain", handle_admin_drain)
    app.router.add_get("/v0/admin/metrics", handle_admin_metrics)

    async def on_shutdown(app: web.Application) -> None:
        if state.supervisor is not None:
            try:
                await state.supervisor.shutdown_all(timeout=5.0)
            except Exception:
                pass
        for q in list(state._streams):  # noqa: SLF001
            try:
                q.put_nowait({"type": "_close", "data": {}})
            except asyncio.QueueFull:
                pass

    app.on_shutdown.append(on_shutdown)
    return app


async def amain(
    manifest_path: str,
    host: str,
    port: int,
    audit_path: str | None,
    *,
    enable_supervisor: bool = False,
    supervisor_log_dir: str = ".logs",
    auto_reconcile: bool = False,
) -> None:
    app = make_app(
        manifest_path,
        audit_path,
        enable_supervisor=enable_supervisor,
        supervisor_log_dir=supervisor_log_dir,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    sup_msg = " supervisor=on" if enable_supervisor else ""
    print(f"[core] listening on http://{host}:{port}  manifest={manifest_path}{sup_msg}", flush=True)

    state: CoreState = app["state"]

    if enable_supervisor and auto_reconcile and state.supervisor is not None:
        # Boot every node declared in the manifest immediately. The supervisor
        # will restart any that crash. This makes Core a self-contained mesh
        # bootstrap: `python -m core.core --supervisor --auto-reconcile` brings
        # the entire mesh up and keeps it up.
        await asyncio.sleep(0.2)  # give the listener a beat to start accepting registrations
        result = await state.supervisor.reconcile(state.manifest_nodes_raw)
        print(f"[core] auto-reconcile: {json.dumps(result['actions'])}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    print("[core] shutting down", flush=True)
    await runner.cleanup()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RAVEN Mesh Core")
    p.add_argument("--manifest", default=os.environ.get("MESH_MANIFEST", "manifests/demo.yaml"))
    p.add_argument("--host", default=os.environ.get("MESH_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MESH_PORT", "8000")))
    p.add_argument("--audit-log", default=os.environ.get("AUDIT_LOG", "audit.log"))
    p.add_argument(
        "--supervisor",
        action="store_true",
        default=os.environ.get("MESH_SUPERVISOR", "0") == "1",
        help="Enable the in-core process supervisor (own node lifecycle).",
    )
    p.add_argument(
        "--supervisor-log-dir",
        default=os.environ.get("MESH_SUPERVISOR_LOG_DIR", ".logs"),
    )
    p.add_argument(
        "--auto-reconcile",
        action="store_true",
        default=os.environ.get("MESH_AUTO_RECONCILE", "0") == "1",
        help="With --supervisor: spawn all manifest nodes at startup.",
    )
    args = p.parse_args(argv)
    try:
        asyncio.run(amain(
            args.manifest,
            args.host,
            args.port,
            args.audit_log,
            enable_supervisor=args.supervisor,
            supervisor_log_dir=args.supervisor_log_dir,
            auto_reconcile=args.auto_reconcile,
        ))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
