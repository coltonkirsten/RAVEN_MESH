"""Shared ui_visibility helper for nodes that run their own web server.

A node uses this to:
    1) Add a `visibility_middleware` to its aiohttp app — when hidden, every
       request returns 503 except SSE event streams (so the dashboard can
       still observe state changes).
    2) Expose a `ui_visibility` mesh tool surface (handler returned by
       `make_handler`) that flips the local visibility flag.
"""
from __future__ import annotations

import logging
from typing import Iterable

from aiohttp import web

log = logging.getLogger("ui_visibility")

SSE_ALLOWED_PATHS = ("/events",)


class VisibilityState:
    def __init__(self, visible: bool = True) -> None:
        self.visible = visible


def make_visibility_middleware(state: VisibilityState,
                               always_open: Iterable[str] = SSE_ALLOWED_PATHS):
    open_paths = tuple(always_open)

    @web.middleware
    async def middleware(request: web.Request, handler):
        if not state.visible and not request.path.startswith(open_paths):
            return web.json_response(
                {"error": "ui_hidden", "node_visible": False},
                status=503,
            )
        return await handler(request)

    return middleware


def make_handler(state: VisibilityState, *, node_id: str):
    async def on_ui_visibility(env: dict) -> dict:
        action = env.get("payload", {}).get("action")
        if action not in ("show", "hide"):
            return {"ok": False, "reason": "bad_action"}
        state.visible = (action == "show")
        log.info("[%s] ui_visibility -> %s", node_id, action)
        return {"ok": True, "visible": state.visible}

    return on_ui_visibility
