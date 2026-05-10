"""Human node — actor with a dashboard.

Inbox surface receives messages addressed to the human (fire_and_forget).
Dashboard at http://127.0.0.1:8802 shows the inbox live, plus a form to
invoke any surface this node has a relationship to.

The dropdown of allowed targets is populated from the relationships Core
returns at registration (the node's outbound edges).
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

from node_sdk import MeshError, MeshNode

log = logging.getLogger("human_node")
HTML_PATH = pathlib.Path(__file__).resolve().parent / "index.html"


class HumanNode:
    def __init__(self, node: MeshNode, max_messages: int = 100):
        self.node = node
        self.max_messages = max_messages
        self.messages: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()

    @property
    def allowed_targets(self) -> list[str]:
        return [r["to"] for r in self.node.relationships if r["from"] == self.node.node_id]

    def state(self) -> dict:
        return {
            "node_id": self.node.node_id,
            "messages": list(self.messages),
            "allowed_targets": self.allowed_targets,
        }

    async def push(self) -> None:
        snap = self.state()
        for q in list(self.subscribers):
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                pass

    async def on_inbox(self, env: dict) -> None:
        msg = {
            "id": env.get("id"),
            "from": env.get("from"),
            "payload": env.get("payload"),
            "timestamp": env.get("timestamp") or _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        self.messages.insert(0, msg)
        del self.messages[self.max_messages:]
        await self.push()
        log.info("inbox <- %s: %s", msg["from"], json.dumps(msg["payload"])[:200])


def make_web_app(human: HumanNode) -> web.Application:
    app = web.Application()

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=HTML_PATH.read_text(), content_type="text/html")

    async def state(request: web.Request) -> web.Response:
        return web.json_response(human.state())

    async def events(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
        })
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue()
        human.subscribers.add(queue)
        try:
            await response.write(f"event: state\ndata: {json.dumps(human.state())}\n\n".encode())
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
            human.subscribers.discard(queue)
        return response

    async def send(request: web.Request) -> web.Response:
        body = await request.json()
        target = body.get("target")
        payload = body.get("payload", {})
        wait = bool(body.get("wait", True))
        if not target:
            return web.json_response({"error": "missing_target"}, status=400)
        try:
            result = await human.node.invoke(target, payload, wait=wait)
            return web.json_response(result)
        except MeshError as e:
            return web.json_response({"error": e.data, "status": e.status}, status=400)
        except Exception as e:
            return web.json_response({"error": "exception", "details": str(e)}, status=500)

    app.router.add_get("/", index)
    app.router.add_get("/state", state)
    app.router.add_get("/events", events)
    app.router.add_post("/send", send)
    return app


async def run(node_id: str, secret: str, core_url: str, web_host: str, web_port: int) -> int:
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.connect()
    human = HumanNode(node)
    node.on("inbox", human.on_inbox)
    await node.serve()

    web_app = make_web_app(human)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, web_host, web_port)
    await site.start()
    print(f"[{node_id}] human_node ready. dashboard: http://{web_host}:{web_port}", flush=True)
    print(f"[{node_id}] allowed targets: {human.allowed_targets}", flush=True)

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
    p.add_argument("--node-id", default="human_node")
    p.add_argument("--secret-env", default=None)
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--web-host", default=os.environ.get("HUMAN_HOST", "127.0.0.1"))
    p.add_argument("--web-port", type=int, default=int(os.environ.get("HUMAN_PORT", "8802")))
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
