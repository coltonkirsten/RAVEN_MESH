"""RAVEN Mesh — single-process Python Core.

Implements the v0 wire protocol (PRD §5). Loads a manifest, listens on HTTP,
verifies HMAC signatures, validates payloads against per-surface JSON Schemas,
and routes messages between connected nodes via SSE delivery + POST responses.
Audit log is JSON-per-line.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import os
import pathlib
import signal
import sys
import uuid
from typing import Any

import yaml
from aiohttp import web
from jsonschema import ValidationError, validate as jsonschema_validate


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


# ---------- state ----------

class CoreState:
    def __init__(self, manifest_path: str, audit_path: str):
        self.manifest_path = pathlib.Path(manifest_path).resolve()
        self.audit_path = pathlib.Path(audit_path).resolve()
        # node_id -> {kind, runtime, metadata, secret, surfaces:{name:{type,schema,invocation_mode}}}
        self.nodes_decl: dict[str, dict] = {}
        # node_id -> {session_id, queue, connected_at}
        self.connections: dict[str, dict] = {}
        # session_id -> node_id
        self.sessions: dict[str, str] = {}
        # set of (from_node, "node_id.surface_name")
        self.edges: set[tuple[str, str]] = set()
        # msg_id -> {future, target_node}
        self.pending: dict[str, dict] = {}
        self.audit_lock = asyncio.Lock()
        # active SSE queues, used to push _close events on shutdown
        self._streams: set[asyncio.Queue] = set()

    # manifest -----------------------------------------------------------

    def load_manifest(self) -> None:
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
        for rel in m.get("relationships", []):
            self.edges.add((rel["from"], rel["to"]))

    def _resolve_secret(self, node_id: str, spec: str) -> str:
        if spec.startswith("env:"):
            var = spec[4:]
            val = os.environ.get(var)
            if val:
                return val
            # auto-generate stable secret if env var unset (prototype convenience)
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

    # audit --------------------------------------------------------------

    async def audit(self, **fields: Any) -> None:
        evt = {"id": str(uuid.uuid4()), "timestamp": now_iso(), **fields}
        line = json.dumps(evt) + "\n"
        async with self.audit_lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a") as f:
                f.write(line)


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
    # Replace any previous session.
    old = state.connections.get(node_id)
    if old:
        state.sessions.pop(old["session_id"], None)
        try:
            old["queue"].put_nowait({"type": "_close", "data": {}})
        except asyncio.QueueFull:
            pass
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
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


async def handle_invoke(request: web.Request) -> web.Response:
    state: CoreState = request.app["state"]
    env = await request.json()
    msg_id = env.get("id") or str(uuid.uuid4())
    env.setdefault("id", msg_id)
    correlation_id = env.get("correlation_id") or msg_id
    env.setdefault("correlation_id", correlation_id)
    from_node = env.get("from")
    to = env.get("to")
    if env.get("kind") not in (None, "invocation"):
        return web.json_response({"error": "bad_kind", "expected": "invocation"}, status=400)
    decl = state.nodes_decl.get(from_node) if from_node else None
    if not decl:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_unknown_node", correlation_id=correlation_id, details={})
        return web.json_response({"error": "unknown_node"}, status=404)
    if not verify(env, decl["secret"]):
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_signature_invalid", correlation_id=correlation_id, details={})
        return web.json_response({"error": "bad_signature"}, status=401)
    if not isinstance(to, str) or "." not in to:
        return web.json_response({"error": "bad_surface_id"}, status=400)
    if (from_node, to) not in state.edges:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_no_relationship", correlation_id=correlation_id, details={})
        return web.json_response({"error": "denied_no_relationship", "from": from_node, "to": to}, status=403)
    target_node, surface_name = to.split(".", 1)
    target_decl = state.nodes_decl.get(target_node)
    if not target_decl or surface_name not in target_decl["surfaces"]:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_unknown_surface", correlation_id=correlation_id, details={})
        return web.json_response({"error": "unknown_surface"}, status=404)
    surface = target_decl["surfaces"][surface_name]
    try:
        jsonschema_validate(env.get("payload", {}), surface["schema"])
    except ValidationError as e:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_schema_invalid", correlation_id=correlation_id,
                          details={"error": str(e)[:500]})
        return web.json_response({"error": "denied_schema_invalid", "details": str(e)}, status=400)
    target_conn = state.connections.get(target_node)
    if not target_conn:
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="denied_node_unreachable", correlation_id=correlation_id, details={})
        return web.json_response({"error": "denied_node_unreachable", "node": target_node}, status=503)
    await state.audit(type="invocation", from_node=from_node, to_surface=to,
                      decision="routed", correlation_id=correlation_id, details={"msg_id": msg_id})
    deliver_event = {"type": "deliver", "data": env}
    if surface["invocation_mode"] == "fire_and_forget":
        await target_conn["queue"].put(deliver_event)
        return web.json_response({"id": msg_id, "status": "accepted"}, status=202)
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending[msg_id] = {"future": fut, "target_node": target_node, "from_node": from_node}
    await target_conn["queue"].put(deliver_event)
    timeout = float(os.environ.get("MESH_INVOKE_TIMEOUT", "30"))
    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        state.pending.pop(msg_id, None)
        await state.audit(type="invocation", from_node=from_node, to_surface=to,
                          decision="timeout", correlation_id=correlation_id, details={})
        return web.json_response({"error": "timeout", "id": msg_id}, status=504)
    finally:
        state.pending.pop(msg_id, None)
    return web.json_response(result, status=200)


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
                # Re-queue so a future stream can pick this up if the node reconnects.
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
    """Read-only registry view. Useful for debugging and the human dashboard."""
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


# ---------- bootstrap ----------

def make_app(manifest_path: str, audit_path: str | None = None) -> web.Application:
    audit_path = audit_path or os.environ.get("AUDIT_LOG", "audit.log")
    app = web.Application(client_max_size=10 * 1024 * 1024)
    state = CoreState(manifest_path, audit_path)
    state.load_manifest()
    app["state"] = state
    app.router.add_post("/v0/register", handle_register)
    app.router.add_post("/v0/invoke", handle_invoke)
    app.router.add_post("/v0/respond", handle_respond)
    app.router.add_get("/v0/stream", handle_stream)
    app.router.add_get("/v0/healthz", handle_health)
    app.router.add_get("/v0/introspect", handle_introspect)

    async def on_shutdown(app: web.Application) -> None:
        for q in list(state._streams):  # noqa: SLF001
            try:
                q.put_nowait({"type": "_close", "data": {}})
            except asyncio.QueueFull:
                pass

    app.on_shutdown.append(on_shutdown)
    return app


async def amain(manifest_path: str, host: str, port: int, audit_path: str | None) -> None:
    app = make_app(manifest_path, audit_path)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"[core] listening on http://{host}:{port}  manifest={manifest_path}", flush=True)
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
    args = p.parse_args(argv)
    try:
        asyncio.run(amain(args.manifest, args.host, args.port, args.audit_log))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
