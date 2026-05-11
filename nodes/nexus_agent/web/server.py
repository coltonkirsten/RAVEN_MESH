"""Inspector UI server for the Nexus Agent — http://localhost:8804.

A single-page dashboard that streams every event from the agent runtime
(incoming messages, claude turns, tool calls, tool results, errors) over SSE
to a thin vanilla-JS frontend. Also exposes JSON read endpoints for the
ledger files and the MCP tool list.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import pathlib
from typing import TYPE_CHECKING, Any

from aiohttp import web

from node_sdk.inspector.sse import SSEHub, serve_sse

if TYPE_CHECKING:
    from ..agent import AgentRuntime


HTML_PATH = pathlib.Path(__file__).resolve().parent / "index.html"


@dataclasses.dataclass
class AgentInspectorState:
    node_id: str
    ledger_dir: pathlib.Path
    skills_dir: pathlib.Path
    logs_dir: pathlib.Path
    control_port: int
    model: str
    last_result: dict | None = None
    runtime: "AgentRuntime | None" = None
    hub: SSEHub = dataclasses.field(default_factory=SSEHub)
    history: list[dict] = dataclasses.field(default_factory=list)
    history_max: int = 500

    async def publish(self, kind: str, data: Any) -> None:
        evt = {
            "kind": kind,
            "data": data,
            "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        self.history.append(evt)
        if len(self.history) > self.history_max:
            self.history = self.history[-self.history_max:]
        # The data field on the wire is the full event dict — the legacy
        # frontend reads `evt.kind` and `evt.at` from inside `data`.
        self.hub.broadcast(kind, evt, event_id=evt["at"])


def make_inspector_app(state: AgentInspectorState, rt: "AgentRuntime") -> web.Application:
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
        # Read tool definitions from the bridge module without importing it
        # (the bridge runs in a subprocess; reflection is fine).
        from .. import mcp_bridge  # type: ignore
        out = []
        for t in mcp_bridge.TOOLS:
            out.append({
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
            })
        return web.json_response({"tools": out})

    async def events(request: web.Request) -> web.StreamResponse:
        def replay():
            return [(evt["kind"], evt, evt["at"]) for evt in state.history[-100:]]
        return await serve_sse(request, state.hub, replay=replay, queue_maxsize=512)

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
