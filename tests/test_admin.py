"""Operator endpoint tests — only ``/v0/admin/{stream,metrics}`` survive
SPEC §4.5; every other operator action lives on ``core.<surface>`` and is
covered by tests/test_core_surfaces.py.
"""
from __future__ import annotations

import asyncio
import json
import os

import aiohttp
import pytest

from node_sdk import MeshNode

from tests.conftest import TEST_ADMIN_TOKEN as ADMIN_TOKEN

HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


async def _spawn_tasks_node(core_url: str):
    secret = os.environ["TASKS_SECRET"]
    node = MeshNode(node_id="tasks", secret=secret, core_url=core_url)
    await node.connect()

    async def on_list(env: dict) -> dict:
        return {"tasks": []}

    async def on_create(env: dict) -> dict:
        return {"created": {"id": "x", "title": env["payload"].get("title")}}

    node.on("list", on_list)
    node.on("create", on_create)
    await node.serve()
    return node


async def test_admin_stream_delivers_envelope_events(core_server):
    url = core_server["url"]
    tasks = await _spawn_tasks_node(url)
    voice = MeshNode(node_id="voice_actor",
                      secret=os.environ["VOICE_SECRET"], core_url=url)
    await voice.start()
    received: list[dict] = []
    seen = asyncio.Event()

    async def consume():
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url}/v0/admin/stream", headers=HEADERS,
                              timeout=aiohttp.ClientTimeout(total=None)) as r:
                assert r.status == 200
                event_type = None
                buf: list[str] = []
                async for raw in r.content:
                    line = raw.decode().rstrip("\r\n")
                    if line == "":
                        if event_type == "envelope" and buf:
                            received.append(json.loads("\n".join(buf)))
                            if any(e.get("to_surface") == "tasks.list" for e in received):
                                seen.set()
                                return
                        event_type = None
                        buf = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        buf.append(line[5:].lstrip())

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0.1)
        await voice.invoke("tasks.list", {})
        await asyncio.wait_for(seen.wait(), timeout=5)
        match = next(e for e in received if e.get("to_surface") == "tasks.list")
        assert match["from_node"] == "voice_actor"
        assert match["route_status"] == "routed"
        assert match["signature_valid"] is True
        assert match["direction"] == "in"
    finally:
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass
        await asyncio.gather(tasks.stop(), voice.stop())


async def test_admin_stream_requires_auth(core_server):
    url = core_server["url"]
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/v0/admin/stream") as r:
            assert r.status == 401


async def test_admin_metrics_returns_prometheus(core_server):
    url = core_server["url"]
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/v0/admin/metrics", headers=HEADERS) as r:
            assert r.status == 200
            assert r.headers["Content-Type"].startswith("text/plain")
            body = await r.text()
    # Spot-check a representative metric line and the HELP/TYPE preamble.
    assert "# TYPE mesh_nodes_declared gauge" in body
    assert "mesh_nodes_declared " in body
    assert "mesh_edges " in body
    assert "mesh_replay_window_seconds " in body


async def test_admin_metrics_requires_auth(core_server):
    url = core_server["url"]
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/v0/admin/metrics") as r:
            assert r.status == 401


async def test_removed_admin_endpoints_404(core_server):
    """SPEC §4.5: state/manifest/reload/invoke/spawn/etc. no longer exist."""
    url = core_server["url"]
    gone = [
        ("GET", "/v0/admin/state"),
        ("POST", "/v0/admin/manifest"),
        ("POST", "/v0/admin/reload"),
        ("POST", "/v0/admin/invoke"),
        ("GET", "/v0/admin/processes"),
        ("POST", "/v0/admin/spawn"),
        ("POST", "/v0/admin/stop"),
        ("POST", "/v0/admin/restart"),
        ("POST", "/v0/admin/reconcile"),
        ("POST", "/v0/admin/drain"),
    ]
    async with aiohttp.ClientSession() as s:
        for method, path in gone:
            async with s.request(method, f"{url}{path}", headers=HEADERS,
                                  json={}) as r:
                assert r.status == 404, f"{method} {path} expected 404, got {r.status}"


async def test_admin_rejects_query_string_token(core_server):
    """Header-only auth: query-string tokens leak through logs/Referer."""
    url = core_server["url"]
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/v0/admin/metrics",
                          params={"admin_token": ADMIN_TOKEN}) as r:
            assert r.status == 401


def _trivial_manifest(tmp_path):
    """Build a one-node ephemeral manifest. The tests using this don't care
    about the mesh shape — they care about make_app's boot-time invariants
    (ADMIN_TOKEN gating, rate limiting). Anything that loads is enough.
    """
    from tests._mesh_helpers import (
        build_ephemeral_manifest, minimal_actor, minimal_surface,
    )
    return build_ephemeral_manifest(
        tmp_path,
        [minimal_actor("solo", surfaces=[minimal_surface("inbox")])],
    )


async def test_admin_token_boot_check_refuses_unset(monkeypatch, tmp_path):
    """make_app must refuse to start with no ADMIN_TOKEN."""
    from core.core import make_app as _make_app
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    manifest = _trivial_manifest(tmp_path)
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        _make_app(str(manifest), str(tmp_path / "audit.log"))


async def test_admin_token_boot_check_refuses_legacy_default(monkeypatch, tmp_path):
    """make_app must refuse to start with the legacy 'admin-dev-token'."""
    from core.core import make_app as _make_app
    monkeypatch.setenv("ADMIN_TOKEN", "admin-dev-token")
    manifest = _trivial_manifest(tmp_path)
    with pytest.raises(RuntimeError, match="legacy placeholder"):
        _make_app(str(manifest), str(tmp_path / "audit.log"))


async def test_admin_rate_limit_returns_429(monkeypatch, tmp_path):
    """Token bucket on /v0/admin/* returns 429 once burst exhausts."""
    import socket as _socket
    from aiohttp import web as _web
    from core.core import make_app as _make_app

    monkeypatch.setenv("MESH_ADMIN_RATE_LIMIT", "60")
    monkeypatch.setenv("MESH_ADMIN_RATE_BURST", "3")
    manifest = _trivial_manifest(tmp_path)
    app = _make_app(str(manifest), str(tmp_path / "audit.log"))
    runner = _web.AppRunner(app)
    await runner.setup()
    s = _socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    site = _web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        url = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            statuses = []
            for _ in range(8):
                async with session.get(f"{url}/v0/admin/metrics", headers=HEADERS) as r:
                    statuses.append(r.status)
        assert 429 in statuses, f"expected 429 in {statuses}"
        assert statuses.index(429) <= 5  # burst=3 plus a few refills
    finally:
        await runner.cleanup()


async def test_node_queue_is_bounded(core_server):
    """Per-node delivery queue is capped; overflow yields denied_queue_full."""
    import asyncio as _asyncio
    from core.core import NODE_QUEUE_MAX

    target_queue = _asyncio.Queue(maxsize=NODE_QUEUE_MAX)
    while not target_queue.full():
        target_queue.put_nowait({"type": "deliver", "data": {}})
    assert target_queue.qsize() == NODE_QUEUE_MAX
    with pytest.raises(_asyncio.QueueFull):
        target_queue.put_nowait({"type": "deliver", "data": {}})
