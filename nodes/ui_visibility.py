"""Shared ui_visibility helper for nodes that run their own web server.

A node uses this to:
    1) Add a `visibility_middleware` to its aiohttp app — when hidden, every
       request returns 503 except SSE event streams (so the dashboard can
       still observe state changes).
    2) Expose a `ui_visibility` mesh tool surface (handler returned by
       `make_handler`) that flips the flag and reports the new state to Core
       via /v0/admin/node_status.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

import aiohttp
from aiohttp import web

log = logging.getLogger("ui_visibility")

DEFAULT_ADMIN_TOKEN = "admin-dev-token"
SSE_ALLOWED_PATHS = ("/events",)


def admin_token() -> str:
    return os.environ.get("ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)


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


async def report_status(node_id: str, visible: bool, *, core_url: str) -> None:
    body = {"node_id": node_id, "visible": visible}
    headers = {"X-Admin-Token": admin_token()}
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{core_url}/v0/admin/node_status",
                               json=body, headers=headers) as r:
                if r.status != 200:
                    log.warning("[%s] node_status report rejected: %s", node_id, r.status)
    except Exception as e:
        log.warning("[%s] node_status report failed: %s", node_id, e)


def make_handler(state: VisibilityState, *, node_id: str, core_url: str):
    async def on_ui_visibility(env: dict) -> dict:
        action = env.get("payload", {}).get("action")
        if action not in ("show", "hide"):
            return {"ok": False, "reason": "bad_action"}
        state.visible = (action == "show")
        log.info("[%s] ui_visibility -> %s", node_id, action)
        # Fire-and-forget the status report.
        asyncio.create_task(report_status(node_id, state.visible, core_url=core_url))
        return {"ok": True, "visible": state.visible}

    return on_ui_visibility
