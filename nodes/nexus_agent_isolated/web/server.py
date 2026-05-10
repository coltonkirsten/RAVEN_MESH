"""Inspector UI server for nexus_agent_isolated.

Reuses AgentInspectorState from nexus_agent.web.server but serves a local
copy of index.html (re-titled) and reads tool definitions from this node's
own mcp_bridge module.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
from typing import TYPE_CHECKING

from aiohttp import web

from ..  import mcp_bridge as _bridge  # local bridge (sibling)
from nodes.nexus_agent.web.server import AgentInspectorState  # noqa: F401

if TYPE_CHECKING:
    from ..agent import AgentRuntime


HTML_PATH = pathlib.Path(__file__).resolve().parent / "index.html"


def make_inspector_app(state: "AgentInspectorState", rt: "AgentRuntime") -> web.Application:
    app = web.Application()

    async def index(request: web.Request) -> web.Response:
        if not rt.ui_visible:
            return web.Response(status=503, text="ui hidden")
        return web.Response(text=HTML_PATH.read_text(), content_type="text/html")

    async def status(request: web.Request) -> web.Response:
        return web.json_response({
            "node_id": state.node_id,
            "model": state.model,
            "session_id": rt.session_id,
            "run_count": rt.run_count,
            "ui_visible": rt.ui_visible,
            "last_result": state.last_result,
            "control_port": state.control_port,
        })

    async def history(request: web.Request) -> web.Response:
        return web.json_response({"events": state.history[-200:]})

    async def memory(request: web.Request) -> web.Response:
        path = state.ledger_dir / "memory.md"
        try:
            return web.json_response({"content": path.read_text()})
        except FileNotFoundError:
            return web.json_response({"content": ""})

    async def identity(request: web.Request) -> web.Response:
        path = state.ledger_dir / "identity.md"
        try:
            return web.json_response({"content": path.read_text()})
        except FileNotFoundError:
            return web.json_response({"content": ""})

    async def skills(request: web.Request) -> web.Response:
        if not state.skills_dir.exists():
            return web.json_response({"skills": []})
        names = sorted(p.name for p in state.skills_dir.glob("*.md"))
        return web.json_response({"skills": names})

    async def skill_get(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if not name.endswith(".md"):
            name = f"{name}.md"
        path = state.skills_dir / name
        if not path.resolve().is_relative_to(state.skills_dir.resolve()):
            return web.json_response({"error": "bad path"}, status=400)
        try:
            return web.json_response({"name": name, "content": path.read_text()})
        except FileNotFoundError:
            return web.json_response({"error": "not found"}, status=404)

    async def tools(request: web.Request) -> web.Response:
        out = []
        for t in _bridge.TOOLS:
            out.append({
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
            })
        return web.json_response({"tools": out})

    async def events(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
        })
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        state.subscribers.add(queue)
        try:
            for evt in state.history[-100:]:
                await response.write(
                    f"event: {evt['kind']}\ndata: {json.dumps(evt, default=str)}\n\n".encode()
                )
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    try:
                        await response.write(b": heartbeat\n\n")
                    except (ConnectionResetError, BrokenPipeError):
                        break
                    continue
                try:
                    await response.write(
                        f"event: {evt['kind']}\ndata: {json.dumps(evt, default=str)}\n\n".encode()
                    )
                except (ConnectionResetError, BrokenPipeError):
                    break
        finally:
            state.subscribers.discard(queue)
        return response

    app.router.add_get("/", index)
    app.router.add_get("/status", status)
    app.router.add_get("/history", history)
    app.router.add_get("/memory", memory)
    app.router.add_get("/identity", identity)
    app.router.add_get("/skills", skills)
    app.router.add_get("/skills/{name}", skill_get)
    app.router.add_get("/tools", tools)
    app.router.add_get("/events", events)
    return app
