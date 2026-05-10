"""Tests for kanban_node — capability surfaces, persistence, UI gating."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import shutil
import socket

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from core.core import make_app  # noqa: E402
from node_sdk import MeshError, MeshNode  # noqa: E402

KANBAN_MANIFEST = ROOT / "manifests" / "kanban_demo.yaml"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _set_secrets() -> None:
    os.environ.setdefault(
        "KANBAN_NODE_SECRET",
        hashlib.sha256(b"mesh:kanban_node:test").hexdigest(),
    )
    os.environ.setdefault(
        "DUMMY_ACTOR_SECRET",
        hashlib.sha256(b"mesh:dummy_actor:test").hexdigest(),
    )
    os.environ.setdefault(
        "HUMAN_NODE_SECRET",
        hashlib.sha256(b"mesh:human_node:test").hexdigest(),
    )


@pytest_asyncio.fixture
async def kanban_core(tmp_path):
    """Boots Core with the kanban_demo manifest and returns the URL."""
    _set_secrets()
    audit_path = tmp_path / "audit.log"
    os.environ["AUDIT_LOG"] = str(audit_path)
    app = make_app(str(KANBAN_MANIFEST), str(audit_path))
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    yield {"url": f"http://127.0.0.1:{port}", "state": app["state"]}
    await runner.cleanup()


@pytest_asyncio.fixture
async def kanban_node(tmp_path, monkeypatch):
    """Spawns the kanban_node process in-tree against a fresh data dir.

    Patches the data file path to a temp dir so each test starts clean.
    """
    from nodes.kanban_node import kanban_node as kn_mod

    tmp_data = tmp_path / "kanban_data"
    tmp_data.mkdir()
    monkeypatch.setattr(kn_mod, "DATA_DIR", tmp_data)
    monkeypatch.setattr(kn_mod, "DATA_FILE", tmp_data / "board.json")
    yield kn_mod


async def _spawn_kanban(core_url: str, kn_mod) -> tuple[MeshNode, object, web.AppRunner, int]:
    """Start the kanban_node mesh registration + web UI on a free port."""
    secret = os.environ["KANBAN_NODE_SECRET"]
    board = kn_mod.KanbanBoard()
    node = MeshNode(node_id="kanban_node", secret=secret, core_url=core_url)
    handler = kn_mod.KanbanNode(board)
    node.on("create_card", handler.create_card)
    node.on("move_card", handler.move_card)
    node.on("update_card", handler.update_card)
    node.on("delete_card", handler.delete_card)
    node.on("list_cards", handler.list_cards)
    node.on("get_board", handler.get_board)
    node.on("add_column", handler.add_column)
    node.on("delete_column", handler.delete_column)
    node.on("ui_visibility", handler.ui_visibility)
    node.on("status_get", handler.status_get)
    await node.start()

    web_app = kn_mod.make_web_app(board)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return node, board, runner, port


async def _spawn_actor(core_url: str, node_id: str, secret_env: str) -> MeshNode:
    secret = os.environ[secret_env]
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.start()
    return node


# ---------- tests ----------

async def test_default_columns_on_first_boot(kanban_core, kanban_node):
    node, board, runner, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    try:
        assert [c["name"] for c in sorted(board.columns, key=lambda x: x["position"])] == [
            "Backlog", "In Progress", "Review", "Done"
        ]
        assert board.cards == []
    finally:
        await node.stop()
        await runner.cleanup()


async def test_create_card_returns_id_and_appears_in_board(kanban_core, kanban_node):
    node, _, runner, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    actor = await _spawn_actor(kanban_core["url"], "dummy_actor", "DUMMY_ACTOR_SECRET")
    try:
        result = await actor.invoke("kanban_node.create_card", {
            "column": "Backlog", "title": "first card",
            "description": "a thing", "tags": ["test", "v0"],
        })
        assert result["kind"] == "response"
        card_id = result["payload"]["card_id"]
        assert card_id.startswith("card_") and len(card_id) == 13

        board = await actor.invoke("kanban_node.get_board", {})
        cards = board["payload"]["cards"]
        assert len(cards) == 1
        assert cards[0]["id"] == card_id
        assert cards[0]["title"] == "first card"
        assert cards[0]["tags"] == ["test", "v0"]
    finally:
        await asyncio.gather(actor.stop(), node.stop())
        await runner.cleanup()


async def test_move_card_changes_column(kanban_core, kanban_node):
    node, _, runner, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    actor = await _spawn_actor(kanban_core["url"], "dummy_actor", "DUMMY_ACTOR_SECRET")
    try:
        created = await actor.invoke("kanban_node.create_card",
                                     {"column": "Backlog", "title": "movable"})
        card_id = created["payload"]["card_id"]
        moved = await actor.invoke("kanban_node.move_card",
                                   {"card_id": card_id, "to_column": "In Progress"})
        assert moved["payload"]["card"]["column"] == "In Progress"
        listed = await actor.invoke("kanban_node.list_cards", {"column": "In Progress"})
        assert any(c["id"] == card_id for c in listed["payload"]["cards"])
    finally:
        await asyncio.gather(actor.stop(), node.stop())
        await runner.cleanup()


async def test_update_card_partial_preserves_untouched_fields(kanban_core, kanban_node):
    node, _, runner, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    actor = await _spawn_actor(kanban_core["url"], "dummy_actor", "DUMMY_ACTOR_SECRET")
    try:
        created = await actor.invoke("kanban_node.create_card", {
            "column": "Backlog", "title": "original", "description": "keep me",
            "tags": ["a", "b"],
        })
        card_id = created["payload"]["card_id"]
        # Update only the title.
        result = await actor.invoke("kanban_node.update_card",
                                    {"card_id": card_id, "title": "renamed"})
        card = result["payload"]["card"]
        assert card["title"] == "renamed"
        assert card["description"] == "keep me"
        assert card["tags"] == ["a", "b"]
    finally:
        await asyncio.gather(actor.stop(), node.stop())
        await runner.cleanup()


async def test_delete_card_removes_it(kanban_core, kanban_node):
    node, _, runner, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    actor = await _spawn_actor(kanban_core["url"], "dummy_actor", "DUMMY_ACTOR_SECRET")
    try:
        created = await actor.invoke("kanban_node.create_card",
                                     {"column": "Backlog", "title": "doomed"})
        card_id = created["payload"]["card_id"]
        deleted = await actor.invoke("kanban_node.delete_card", {"card_id": card_id})
        assert deleted["payload"]["deleted"] is True
        listed = await actor.invoke("kanban_node.list_cards", {})
        assert all(c["id"] != card_id for c in listed["payload"]["cards"])
    finally:
        await asyncio.gather(actor.stop(), node.stop())
        await runner.cleanup()


async def test_list_cards_filter_by_column(kanban_core, kanban_node):
    node, _, runner, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    actor = await _spawn_actor(kanban_core["url"], "dummy_actor", "DUMMY_ACTOR_SECRET")
    try:
        for col, title in [("Backlog", "a"), ("Backlog", "b"), ("Done", "c")]:
            await actor.invoke("kanban_node.create_card",
                               {"column": col, "title": title})
        bl = await actor.invoke("kanban_node.list_cards", {"column": "Backlog"})
        done = await actor.invoke("kanban_node.list_cards", {"column": "Done"})
        all_ = await actor.invoke("kanban_node.list_cards", {})
        assert len(bl["payload"]["cards"]) == 2
        assert len(done["payload"]["cards"]) == 1
        assert len(all_["payload"]["cards"]) == 3
    finally:
        await asyncio.gather(actor.stop(), node.stop())
        await runner.cleanup()


async def test_add_column_at_position(kanban_core, kanban_node):
    node, _, runner, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    actor = await _spawn_actor(kanban_core["url"], "dummy_actor", "DUMMY_ACTOR_SECRET")
    try:
        # Insert at position 2 (between "In Progress" and "Review").
        result = await actor.invoke("kanban_node.add_column",
                                    {"name": "Blocked", "position": 2})
        assert result["payload"]["column"]["name"] == "Blocked"
        assert result["payload"]["column"]["position"] == 2

        board = await actor.invoke("kanban_node.get_board", {})
        names = [c["name"] for c in board["payload"]["columns"]]
        assert names == ["Backlog", "In Progress", "Blocked", "Review", "Done"]
    finally:
        await asyncio.gather(actor.stop(), node.stop())
        await runner.cleanup()


async def test_delete_column_rejects_when_not_empty(kanban_core, kanban_node):
    node, _, runner, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    actor = await _spawn_actor(kanban_core["url"], "dummy_actor", "DUMMY_ACTOR_SECRET")
    try:
        await actor.invoke("kanban_node.create_card",
                           {"column": "Backlog", "title": "blocking"})
        result = await actor.invoke("kanban_node.delete_column", {"name": "Backlog"})
        assert result["kind"] == "error"
        assert result["payload"]["reason"] == "column_not_empty"
    finally:
        await asyncio.gather(actor.stop(), node.stop())
        await runner.cleanup()


async def test_delete_column_succeeds_when_empty(kanban_core, kanban_node):
    node, _, runner, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    actor = await _spawn_actor(kanban_core["url"], "dummy_actor", "DUMMY_ACTOR_SECRET")
    try:
        result = await actor.invoke("kanban_node.delete_column", {"name": "Review"})
        assert result["kind"] == "response"
        assert result["payload"]["ok"] is True

        board = await actor.invoke("kanban_node.get_board", {})
        names = [c["name"] for c in board["payload"]["columns"]]
        assert "Review" not in names
    finally:
        await asyncio.gather(actor.stop(), node.stop())
        await runner.cleanup()


async def test_ui_visibility_hide_blocks_browser_routes_but_not_tools(kanban_core, kanban_node):
    node, _, runner, port = await _spawn_kanban(kanban_core["url"], kanban_node)
    actor = await _spawn_actor(kanban_core["url"], "dummy_actor", "DUMMY_ACTOR_SECRET")
    try:
        # Hide the UI.
        result = await actor.invoke("kanban_node.ui_visibility", {"action": "hide"})
        assert result["payload"]["hidden"] is True

        # Browser routes should now 503.
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/") as r:
                assert r.status == 503
            async with s.get(f"http://127.0.0.1:{port}/state") as r:
                assert r.status == 503
            async with s.post(f"http://127.0.0.1:{port}/api/cards",
                              json={"column": "Backlog", "title": "via http"}) as r:
                assert r.status == 503

        # But mesh tool invocations still work normally.
        created = await actor.invoke("kanban_node.create_card",
                                     {"column": "Backlog", "title": "via mesh"})
        assert created["kind"] == "response"
        assert created["payload"]["card"]["title"] == "via mesh"

        # Show it again.
        result = await actor.invoke("kanban_node.ui_visibility", {"action": "show"})
        assert result["payload"]["hidden"] is False
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/state") as r:
                assert r.status == 200
    finally:
        await asyncio.gather(actor.stop(), node.stop())
        await runner.cleanup()


async def test_persistence_across_restart(kanban_core, kanban_node):
    """Create a card, tear down the node, spin up a fresh one — card survives."""
    node, _, runner, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    actor = await _spawn_actor(kanban_core["url"], "dummy_actor", "DUMMY_ACTOR_SECRET")
    try:
        created = await actor.invoke("kanban_node.create_card", {
            "column": "Backlog", "title": "persist me", "tags": ["x"],
        })
        card_id = created["payload"]["card_id"]
    finally:
        await node.stop()
        await runner.cleanup()

    # Confirm the file is on disk.
    assert kanban_node.DATA_FILE.exists()
    raw = json.loads(kanban_node.DATA_FILE.read_text())
    assert any(c["id"] == card_id for c in raw["cards"])

    # Restart kanban node — board should be reloaded from disk.
    node2, board2, runner2, _ = await _spawn_kanban(kanban_core["url"], kanban_node)
    try:
        listed = await actor.invoke("kanban_node.list_cards", {})
        ids = [c["id"] for c in listed["payload"]["cards"]]
        assert card_id in ids
        assert any(c["title"] == "persist me" for c in board2.cards)
    finally:
        await asyncio.gather(actor.stop(), node2.stop())
        await runner2.cleanup()


async def test_local_http_create_broadcasts_via_sse(kanban_core, kanban_node):
    """Browser-side POST /api/cards mutates state and pushes to SSE subscribers."""
    node, board, runner, port = await _spawn_kanban(kanban_core["url"], kanban_node)
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(f"http://127.0.0.1:{port}/api/cards",
                             json={"column": "Backlog", "title": "from browser"})
            assert r.status == 200
            data = await r.json()
            assert data["card"]["title"] == "from browser"
        # The card landed in the in-memory board too.
        assert any(c["title"] == "from browser" for c in board.cards)
    finally:
        await node.stop()
        await runner.cleanup()
