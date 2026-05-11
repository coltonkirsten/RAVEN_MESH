"""Kanban node — capability that exposes a kanban board as tool surfaces.

Other nodes can create cards, move them between columns, edit, delete, list,
add/remove columns, and toggle UI visibility. The same process serves a live
kanban web UI on port 8805 (default) — browser mutations and mesh tool
invocations both go through the same internal mutators, so SSE updates fan
out to all connected browsers regardless of source.

Tool surfaces:
    kanban_node.create_card     {column, title, description?, tags?}     -> {card_id, card}
    kanban_node.move_card       {card_id, to_column}                     -> {ok, card}
    kanban_node.update_card     {card_id, title?, description?, tags?}   -> {ok, card}
    kanban_node.delete_card     {card_id}                                -> {deleted, card_id}
    kanban_node.list_cards      {column?}                                -> {cards}
    kanban_node.get_board       {}                                       -> {columns, cards}
    kanban_node.add_column      {name, position?}                        -> {ok, column}
    kanban_node.delete_column   {name}                                   -> {ok} | error
    kanban_node.ui_visibility   {action}                                 -> {ok, hidden}
    kanban_node.status_get      {}                                       -> {hidden, cards, columns, ...}
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
import uuid

from aiohttp import web

from node_sdk import MeshDeny, MeshNode
from node_sdk.inspector.sse import SSEHub, serve_sse

log = logging.getLogger("kanban_node")
HERE = pathlib.Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
DATA_DIR = HERE / "data"
DATA_FILE = DATA_DIR / "board.json"

DEFAULT_COLUMNS = ["Backlog", "In Progress", "Review", "Done"]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _new_card_id() -> str:
    return "card_" + uuid.uuid4().hex[:8]


class KanbanBoard:
    """In-memory board state, persisted to data/board.json on every mutation."""

    def __init__(self) -> None:
        self.columns: list[dict] = []
        self.cards: list[dict] = []
        self._lock = asyncio.Lock()
        self._ui_hidden = False
        self.hub = SSEHub()
        self._load()

    # persistence -------------------------------------------------------

    def _load(self) -> None:
        if not DATA_FILE.exists():
            self.columns = [{"name": n, "position": i} for i, n in enumerate(DEFAULT_COLUMNS)]
            self.cards = []
            self._save_sync()
            return
        try:
            data = json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            log.exception("board.json corrupt — reinitializing with defaults")
            self.columns = [{"name": n, "position": i} for i, n in enumerate(DEFAULT_COLUMNS)]
            self.cards = []
            self._save_sync()
            return
        self.columns = data.get("columns") or [
            {"name": n, "position": i} for i, n in enumerate(DEFAULT_COLUMNS)
        ]
        self.cards = data.get("cards", [])

    def _save_sync(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"columns": self.columns, "cards": self.cards}, indent=2))
        tmp.replace(DATA_FILE)

    async def _persist_and_push(self) -> None:
        self._save_sync()
        snap = self.snapshot()
        self.hub.broadcast("state", snap, event_id=snap["updated_at"])

    # snapshots ---------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "columns": sorted(self.columns, key=lambda c: c["position"]),
            "cards": list(self.cards),
            "ui_hidden": self._ui_hidden,
            "updated_at": _now(),
        }

    def column_names(self) -> set[str]:
        return {c["name"] for c in self.columns}

    def find_card(self, card_id: str) -> dict | None:
        for c in self.cards:
            if c["id"] == card_id:
                return c
        return None

    # mutators (must be called under self._lock) ------------------------

    async def create_card(self, column: str, title: str,
                          description: str = "", tags: list[str] | None = None) -> dict:
        async with self._lock:
            if column not in self.column_names():
                raise MeshDeny("unknown_column", column=column)
            card = {
                "id": _new_card_id(),
                "column": column,
                "title": title,
                "description": description or "",
                "tags": list(tags or []),
                "created_at": _now(),
                "updated_at": _now(),
            }
            self.cards.append(card)
            await self._persist_and_push()
            return card

    async def move_card(self, card_id: str, to_column: str) -> dict:
        async with self._lock:
            card = self.find_card(card_id)
            if not card:
                raise MeshDeny("unknown_card", card_id=card_id)
            if to_column not in self.column_names():
                raise MeshDeny("unknown_column", column=to_column)
            card["column"] = to_column
            card["updated_at"] = _now()
            await self._persist_and_push()
            return card

    async def update_card(self, card_id: str, *, title: str | None = None,
                          description: str | None = None,
                          tags: list[str] | None = None) -> dict:
        async with self._lock:
            card = self.find_card(card_id)
            if not card:
                raise MeshDeny("unknown_card", card_id=card_id)
            if title is not None:
                card["title"] = title
            if description is not None:
                card["description"] = description
            if tags is not None:
                card["tags"] = list(tags)
            card["updated_at"] = _now()
            await self._persist_and_push()
            return card

    async def delete_card(self, card_id: str) -> bool:
        async with self._lock:
            for i, c in enumerate(self.cards):
                if c["id"] == card_id:
                    self.cards.pop(i)
                    await self._persist_and_push()
                    return True
            return False

    async def add_column(self, name: str, position: int | None = None) -> dict:
        async with self._lock:
            if name in self.column_names():
                raise MeshDeny("column_exists", name=name)
            if position is None:
                position = max((c["position"] for c in self.columns), default=-1) + 1
            else:
                # shift any column at >= position up by 1
                for c in self.columns:
                    if c["position"] >= position:
                        c["position"] += 1
            col = {"name": name, "position": position}
            self.columns.append(col)
            await self._persist_and_push()
            return col

    async def delete_column(self, name: str) -> dict:
        async with self._lock:
            if name not in self.column_names():
                raise MeshDeny("unknown_column", name=name)
            cards_in = [c for c in self.cards if c["column"] == name]
            if cards_in:
                raise MeshDeny("column_not_empty", name=name, cards=len(cards_in))
            self.columns = [c for c in self.columns if c["name"] != name]
            # compact positions
            for i, c in enumerate(sorted(self.columns, key=lambda x: x["position"])):
                c["position"] = i
            await self._persist_and_push()
            return {"name": name}

    async def set_visibility(self, hidden: bool) -> None:
        async with self._lock:
            self._ui_hidden = hidden
            await self._persist_and_push()

    @property
    def ui_hidden(self) -> bool:
        return self._ui_hidden


# ---------- mesh tool handlers ----------

class KanbanNode:
    def __init__(self, board: KanbanBoard) -> None:
        self.board = board

    async def create_card(self, env: dict) -> dict:
        p = env["payload"]
        card = await self.board.create_card(
            p["column"], p["title"],
            description=p.get("description", ""),
            tags=p.get("tags", []),
        )
        return {"card_id": card["id"], "card": card}

    async def move_card(self, env: dict) -> dict:
        p = env["payload"]
        card = await self.board.move_card(p["card_id"], p["to_column"])
        return {"ok": True, "card": card}

    async def update_card(self, env: dict) -> dict:
        p = env["payload"]
        card = await self.board.update_card(
            p["card_id"],
            title=p.get("title"),
            description=p.get("description"),
            tags=p.get("tags"),
        )
        return {"ok": True, "card": card}

    async def delete_card(self, env: dict) -> dict:
        p = env["payload"]
        deleted = await self.board.delete_card(p["card_id"])
        return {"deleted": deleted, "card_id": p["card_id"]}

    async def list_cards(self, env: dict) -> dict:
        p = env.get("payload") or {}
        col = p.get("column")
        if col is None:
            cards = list(self.board.cards)
        else:
            cards = [c for c in self.board.cards if c["column"] == col]
        return {"cards": cards}

    async def get_board(self, env: dict) -> dict:
        return self.board.snapshot()

    async def add_column(self, env: dict) -> dict:
        p = env["payload"]
        col = await self.board.add_column(p["name"], p.get("position"))
        return {"ok": True, "column": col}

    async def delete_column(self, env: dict) -> dict:
        p = env["payload"]
        result = await self.board.delete_column(p["name"])
        return {"ok": True, **result}

    async def ui_visibility(self, env: dict) -> dict:
        action = env["payload"]["action"]
        if action not in ("show", "hide"):
            raise MeshDeny("bad_action", action=action)
        await self.board.set_visibility(hidden=(action == "hide"))
        return {"ok": True, "hidden": self.board.ui_hidden}

    async def status_get(self, env: dict) -> dict:
        return {
            "hidden": self.board.ui_hidden,
            "cards": len(self.board.cards),
            "columns": len(self.board.columns),
            "updated_at": _now(),
        }


# ---------- web app ----------

def make_web_app(board: KanbanBoard) -> web.Application:
    app = web.Application()

    def gated(handler):
        async def wrapped(request: web.Request):
            if board.ui_hidden:
                return web.Response(status=503, text="UI is hidden")
            return await handler(request)
        return wrapped

    async def index(request: web.Request) -> web.Response:
        if board.ui_hidden:
            return web.Response(status=503, text="UI is hidden")
        return web.Response(
            text=(WEB_DIR / "index.html").read_text(),
            content_type="text/html",
        )

    async def style(request: web.Request) -> web.Response:
        path = WEB_DIR / "style.css"
        if not path.exists():
            return web.Response(status=404)
        if board.ui_hidden:
            return web.Response(status=503, text="UI is hidden")
        return web.Response(text=path.read_text(), content_type="text/css")

    async def get_state(request: web.Request) -> web.Response:
        if board.ui_hidden:
            return web.Response(status=503, text="UI is hidden")
        return web.json_response(board.snapshot())

    # SSE stays open even when hidden so browsers see the un-hide event.
    async def events(request: web.Request) -> web.StreamResponse:
        # Snapshot fresh on each connect so a refreshed page sees current state.
        def replay():
            snap = board.snapshot()
            return [("state", snap, snap["updated_at"])]
        return await serve_sse(request, board.hub, replay=replay)

    # ---- local browser-driven REST mutators (gated by visibility) ----

    @gated
    async def api_post_card(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            card = await board.create_card(
                body["column"], body["title"],
                description=body.get("description", ""),
                tags=body.get("tags", []),
            )
        except MeshDeny as d:
            return web.json_response({"error": d.reason, **d.details}, status=400)
        return web.json_response({"card": card})

    @gated
    async def api_patch_card(request: web.Request) -> web.Response:
        card_id = request.match_info["card_id"]
        body = await request.json()
        try:
            if "to_column" in body:
                card = await board.move_card(card_id, body["to_column"])
            else:
                card = await board.update_card(
                    card_id,
                    title=body.get("title"),
                    description=body.get("description"),
                    tags=body.get("tags"),
                )
        except MeshDeny as d:
            return web.json_response({"error": d.reason, **d.details}, status=400)
        return web.json_response({"card": card})

    @gated
    async def api_delete_card(request: web.Request) -> web.Response:
        card_id = request.match_info["card_id"]
        deleted = await board.delete_card(card_id)
        return web.json_response({"deleted": deleted})

    @gated
    async def api_post_column(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            col = await board.add_column(body["name"], body.get("position"))
        except MeshDeny as d:
            return web.json_response({"error": d.reason, **d.details}, status=400)
        return web.json_response({"column": col})

    @gated
    async def api_delete_column(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            result = await board.delete_column(name)
        except MeshDeny as d:
            return web.json_response({"error": d.reason, **d.details}, status=400)
        return web.json_response(result)

    app.router.add_get("/", index)
    app.router.add_get("/style.css", style)
    app.router.add_get("/state", get_state)
    app.router.add_get("/events", events)
    app.router.add_post("/api/cards", api_post_card)
    app.router.add_patch("/api/cards/{card_id}", api_patch_card)
    app.router.add_delete("/api/cards/{card_id}", api_delete_card)
    app.router.add_post("/api/columns", api_post_column)
    app.router.add_delete("/api/columns/{name}", api_delete_column)
    return app


# ---------- bootstrap ----------

async def run(node_id: str, secret: str, core_url: str,
              web_host: str, web_port: int) -> int:
    board = KanbanBoard()
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    kanban = KanbanNode(board)

    node.on("create_card", kanban.create_card)
    node.on("move_card", kanban.move_card)
    node.on("update_card", kanban.update_card)
    node.on("delete_card", kanban.delete_card)
    node.on("list_cards", kanban.list_cards)
    node.on("get_board", kanban.get_board)
    node.on("add_column", kanban.add_column)
    node.on("delete_column", kanban.delete_column)
    node.on("ui_visibility", kanban.ui_visibility)
    node.on("status_get", kanban.status_get)

    await node.start()

    web_app = make_web_app(board)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, web_host, web_port)
    await site.start()
    print(
        f"[{node_id}] kanban_node ready. board: http://{web_host}:{web_port}  "
        f"({len(board.cards)} cards in {len(board.columns)} columns)",
        flush=True,
    )

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
    p.add_argument("--node-id", default="kanban_node")
    p.add_argument("--secret-env", default=None)
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--web-host", default=os.environ.get("KANBAN_HOST", "127.0.0.1"))
    p.add_argument("--web-port", type=int, default=int(os.environ.get("KANBAN_PORT", "8805")))
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
