"""Approval node — human-in-the-loop mediator.

Inbox surface receives wrapped invocations (per PRD §3 approval mechanics).
Web UI on http://127.0.0.1:8803 shows pending approvals as cards. Approve
forwards to the wrapped target; Deny sends an error response back to the
original requester with reason=denied_by_human.

Tool-flow:
    voice_actor -> approval.inbox {target_surface, payload}
        -> human clicks Approve
        -> approval.invoke(target_surface, payload, wrapped=original_env)
        -> response flows back to voice_actor
    voice_actor -> approval.inbox -> human clicks Deny
        -> error response (reason=denied_by_human) back to voice_actor
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import pathlib
import signal
import sys

from aiohttp import web

from node_sdk import MeshDeny, MeshError, MeshNode

log = logging.getLogger("approval_node")
HTML_PATH = pathlib.Path(__file__).resolve().parent / "index.html"


class ApprovalNode:
    def __init__(self, node: MeshNode):
        self.node = node
        # msg_id -> {env, future, created_at}
        self.pending: dict[str, dict] = {}
        self.subscribers: set[asyncio.Queue] = set()

    def state(self) -> dict:
        return {
            "pending": [
                {"env": p["env"], "created_at": p["created_at"]}
                for p in self.pending.values()
            ],
        }

    async def push(self) -> None:
        snap = self.state()
        for q in list(self.subscribers):
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                pass

    async def on_inbox(self, env: dict) -> dict:
        msg_id = env["id"]
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending[msg_id] = {
            "env": env,
            "future": fut,
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        await self.push()
        try:
            decision = await fut
        finally:
            self.pending.pop(msg_id, None)
            await self.push()
        if decision["action"] == "approve":
            target = env["payload"]["target_surface"]
            inner = env["payload"].get("payload", {})
            try:
                result = await self.node.invoke(target, inner, wrapped=env)
            except MeshError as e:
                raise MeshDeny("downstream_error", status=e.status, data=e.data) from e
            if isinstance(result, dict):
                return result.get("payload", result)
            return {"result": result}
        raise MeshDeny("denied_by_human", note=decision.get("reason") or "denied")

    async def decide(self, msg_id: str, action: str, reason: str | None = None) -> bool:
        entry = self.pending.get(msg_id)
        if not entry or entry["future"].done():
            return False
        entry["future"].set_result({"action": action, "reason": reason})
        return True


def make_web_app(approval: ApprovalNode) -> web.Application:
    app = web.Application()

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=HTML_PATH.read_text(), content_type="text/html")

    async def state(request: web.Request) -> web.Response:
        return web.json_response(approval.state())

    async def events(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
        })
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue()
        approval.subscribers.add(queue)
        try:
            await response.write(f"event: state\ndata: {json.dumps(approval.state())}\n\n".encode())
            while True:
                try:
                    snap = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    try:
                        await response.write(b": heartbeat\n\n")
                    except (ConnectionResetError, BrokenPipeError):
                        break
                    continue
                try:
                    await response.write(f"event: state\ndata: {json.dumps(snap)}\n\n".encode())
                except (ConnectionResetError, BrokenPipeError):
                    break
        finally:
            approval.subscribers.discard(queue)
        return response

    async def decide(request: web.Request) -> web.Response:
        body = await request.json()
        msg_id = body.get("msg_id")
        action = body.get("action")
        reason = body.get("reason")
        if action not in ("approve", "deny"):
            return web.json_response({"error": "bad_action"}, status=400)
        ok = await approval.decide(msg_id, action, reason)
        return web.json_response({"ok": ok})

    app.router.add_get("/", index)
    app.router.add_get("/state", state)
    app.router.add_get("/events", events)
    app.router.add_post("/decide", decide)
    return app


async def run(node_id: str, secret: str, core_url: str, web_host: str, web_port: int) -> int:
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url, invoke_timeout=300)
    await node.connect()
    approval = ApprovalNode(node)
    node.on("inbox", approval.on_inbox)
    await node.serve()

    web_app = make_web_app(approval)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, web_host, web_port)
    await site.start()
    print(f"[{node_id}] approval_node ready. dashboard: http://{web_host}:{web_port}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    await runner.cleanup()
    await node.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default="approval_node")
    p.add_argument("--secret-env", default=None)
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--web-host", default=os.environ.get("APPROVAL_HOST", "127.0.0.1"))
    p.add_argument("--web-port", type=int, default=int(os.environ.get("APPROVAL_PORT", "8803")))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    secret_env = args.secret_env or f"{args.node_id.upper()}_SECRET"
    secret = os.environ.get(secret_env)
    if not secret:
        print(f"missing env var {secret_env}", file=sys.stderr)
        return 2
    return asyncio.run(run(args.node_id, secret, args.core_url, args.web_host, args.web_port))


if __name__ == "__main__":
    sys.exit(main())
