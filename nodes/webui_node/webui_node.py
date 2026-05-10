"""WebUI node — capability with show_message/change_color, plus a live HTML page.

Visit http://127.0.0.1:8801 in a browser. Tool calls update the page via SSE.

Tool surfaces:
    webui_node.show_message  {text}        -> {ok: true}
    webui_node.change_color  {hex_color}   -> {ok: true}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import signal
import sys
import datetime as _dt

from aiohttp import web

from node_sdk import MeshNode
from nodes.ui_visibility import (
    VisibilityState,
    make_handler as make_visibility_handler,
    make_visibility_middleware,
)

log = logging.getLogger("webui_node")
HTML_PATH = pathlib.Path(__file__).resolve().parent / "index.html"


class WebUI:
    def __init__(self):
        self.state = {"message": None, "color": None,
                      "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
        self.subscribers: set[asyncio.Queue] = set()

    def _touch(self) -> None:
        self.state["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()

    async def push(self) -> None:
        snapshot = dict(self.state)
        for q in list(self.subscribers):
            try:
                q.put_nowait(snapshot)
            except asyncio.QueueFull:
                pass

    # mesh tool handlers ------------------------------------------------

    async def show_message(self, env: dict) -> dict:
        text = env["payload"]["text"]
        self.state["message"] = text
        self._touch()
        await self.push()
        log.info("show_message: %s", text)
        return {"ok": True, "message": text}

    async def change_color(self, env: dict) -> dict:
        color = env["payload"]["hex_color"]
        self.state["color"] = color
        self._touch()
        await self.push()
        log.info("change_color: %s", color)
        return {"ok": True, "color": color}


def make_web_app(ui: WebUI, visibility: VisibilityState) -> web.Application:
    app = web.Application(middlewares=[make_visibility_middleware(visibility)])

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=HTML_PATH.read_text(), content_type="text/html")

    async def state(request: web.Request) -> web.Response:
        return web.json_response(ui.state)

    async def events(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
        })
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue()
        ui.subscribers.add(queue)
        try:
            await response.write(f"event: state\ndata: {json.dumps(ui.state)}\n\n".encode())
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
            ui.subscribers.discard(queue)
        return response

    app.router.add_get("/", index)
    app.router.add_get("/state", state)
    app.router.add_get("/events", events)
    return app


async def run(node_id: str, secret: str, core_url: str, web_host: str, web_port: int) -> int:
    ui = WebUI()
    visibility = VisibilityState(visible=True)
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    node.on("show_message", ui.show_message)
    node.on("change_color", ui.change_color)
    node.on("ui_visibility", make_visibility_handler(visibility, node_id=node_id))
    await node.start()

    web_app = make_web_app(ui, visibility)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, web_host, web_port)
    await site.start()
    print(f"[{node_id}] webui_node ready. dashboard: http://{web_host}:{web_port}", flush=True)

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
    p.add_argument("--node-id", default="webui_node")
    p.add_argument("--secret-env", default=None)
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--web-host", default=os.environ.get("WEBUI_HOST", "127.0.0.1"))
    p.add_argument("--web-port", type=int, default=int(os.environ.get("WEBUI_PORT", "8801")))
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
