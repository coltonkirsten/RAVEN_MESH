"""Admin endpoint tests — /v0/admin/{state,stream,manifest,reload,invoke,...}."""
from __future__ import annotations

import asyncio
import json
import os

import aiohttp
import pytest

from node_sdk import MeshNode

ADMIN_TOKEN = "admin-dev-token"
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


async def test_admin_state_returns_expected_shape(core_server):
    url = core_server["url"]
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/v0/admin/state", headers=HEADERS) as r:
            assert r.status == 200
            data = await r.json()
    assert "nodes" in data and "relationships" in data and "envelope_tail" in data
    node_ids = {n["id"] for n in data["nodes"]}
    assert {"voice_actor", "tasks", "human_approval"} <= node_ids
    # Each surface comes back with its full schema dict.
    voice = next(n for n in data["nodes"] if n["id"] == "voice_actor")
    assert voice["surfaces"][0]["schema"]["type"] == "object"


async def test_admin_state_requires_auth(core_server):
    url = core_server["url"]
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/v0/admin/state") as r:
            assert r.status == 401
        async with s.get(f"{url}/v0/admin/state",
                          headers={"X-Admin-Token": "wrong"}) as r:
            assert r.status == 401


async def test_admin_stream_delivers_envelope_events(core_server):
    url = core_server["url"]
    tasks = await _spawn_tasks_node(url)
    voice = MeshNode(node_id="voice_actor",
                      secret=os.environ["VOICE_SECRET"], core_url=url)
    await voice.start()
    # Subscribe to stream first.
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
        # Tiny delay so the consumer actually subscribes before we fire.
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


async def test_admin_envelope_tail_records_recent_invocations(core_server):
    url = core_server["url"]
    tasks = await _spawn_tasks_node(url)
    voice = MeshNode(node_id="voice_actor",
                      secret=os.environ["VOICE_SECRET"], core_url=url)
    await voice.start()
    try:
        await voice.invoke("tasks.list", {})
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url}/v0/admin/state", headers=HEADERS) as r:
                data = await r.json()
        tail = data["envelope_tail"]
        assert any(e["to_surface"] == "tasks.list" and e["direction"] == "in"
                   for e in tail)
        assert any(e["direction"] == "out" for e in tail)  # response also tapped
    finally:
        await asyncio.gather(tasks.stop(), voice.stop())


async def test_admin_reload_succeeds(core_server):
    url = core_server["url"]
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/v0/admin/reload", headers=HEADERS) as r:
            assert r.status == 200
            data = await r.json()
            assert data["ok"] is True
            assert data["nodes_declared"] >= 3
        async with s.post(f"{url}/v0/admin/reload") as r:
            assert r.status == 401


async def test_admin_invoke_routes_synthetic_envelope(core_server):
    url = core_server["url"]
    tasks = await _spawn_tasks_node(url)
    voice = MeshNode(node_id="voice_actor",
                      secret=os.environ["VOICE_SECRET"], core_url=url)
    await voice.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/v0/admin/invoke", headers=HEADERS, json={
                "from_node": "voice_actor",
                "target": "tasks.list",
                "payload": {},
            }) as r:
                assert r.status == 200
                data = await r.json()
                assert data["kind"] == "response"
                assert "tasks" in data["payload"]
        # Without auth, denied.
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/v0/admin/invoke", json={
                "from_node": "voice_actor", "target": "tasks.list", "payload": {},
            }) as r:
                assert r.status == 401
    finally:
        await asyncio.gather(tasks.stop(), voice.stop())


async def test_admin_node_status_and_ui_state(core_server):
    url = core_server["url"]
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/v0/admin/node_status", headers=HEADERS, json={
            "node_id": "tasks", "visible": False,
        }) as r:
            assert r.status == 200
        async with s.get(f"{url}/v0/admin/ui_state", headers=HEADERS) as r:
            assert r.status == 200
            data = await r.json()
        assert data["node_status"]["tasks"]["visible"] is False
        # Unknown node rejected.
        async with s.post(f"{url}/v0/admin/node_status", headers=HEADERS, json={
            "node_id": "ghost", "visible": True,
        }) as r:
            assert r.status == 404
