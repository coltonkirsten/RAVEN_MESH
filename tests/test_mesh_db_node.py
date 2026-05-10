"""Tests for mesh_db_node — exposes Core's audit.log as a queryable mesh surface.

Boots a fresh Core with the mesh_db_demo manifest, spawns mesh_db_node and
demo_actor, generates traffic, then asserts query/count/trace responses match.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import pathlib
import socket
import sys

import pytest_asyncio
from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.core import make_app  # noqa: E402
from experiments.mesh_only_ideas.mesh_db.mesh_db_node import (  # noqa: E402
    count_entries,
    load_audit,
    make_handlers,
    matches,
    query_entries,
    trace_entries,
)
from node_sdk import MeshNode  # noqa: E402

MESH_DB_MANIFEST = ROOT / "manifests" / "mesh_db_demo.yaml"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _set_secrets() -> None:
    os.environ.setdefault(
        "MESH_DB_NODE_SECRET",
        hashlib.sha256(b"mesh:mesh_db_node:test").hexdigest(),
    )
    os.environ.setdefault(
        "DEMO_ACTOR_SECRET",
        hashlib.sha256(b"mesh:demo_actor:test").hexdigest(),
    )


@pytest_asyncio.fixture
async def mesh_db_core(tmp_path):
    _set_secrets()
    audit_path = tmp_path / "audit.log"
    os.environ["AUDIT_LOG"] = str(audit_path)
    app = make_app(str(MESH_DB_MANIFEST), str(audit_path))
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base_url = f"http://127.0.0.1:{port}"
    yield {"url": base_url, "audit_path": audit_path, "state": app["state"]}
    await runner.cleanup()


# --- pure helpers ---------------------------------------------------------

def test_matches_filters_by_field():
    e = {"from_node": "a", "to_surface": "b.x", "decision": "routed"}
    assert matches(e, {"from_node": "a"})
    assert matches(e, {"decision": "routed"})
    assert not matches(e, {"from_node": "z"})


def test_matches_since_uses_string_compare():
    e = {"timestamp": "2026-05-10T00:00:00Z"}
    assert matches(e, {"since": "2026-05-09T00:00:00Z"})
    assert not matches(e, {"since": "2026-05-11T00:00:00Z"})


def test_count_groups_by_field():
    es = [{"decision": "routed"}, {"decision": "routed"}, {"decision": "denied_no_relationship"}]
    assert count_entries(es, "decision") == {"routed": 2, "denied_no_relationship": 1}


def test_query_limit_returns_tail():
    es = [{"i": i, "from_node": "a"} for i in range(10)]
    out = query_entries(es, {"from_node": "a"}, limit=3)
    assert [e["i"] for e in out] == [7, 8, 9]


def test_trace_filters_by_correlation_id():
    es = [
        {"correlation_id": "c1", "n": 1},
        {"correlation_id": "c2", "n": 2},
        {"correlation_id": "c1", "n": 3},
    ]
    chain = trace_entries(es, "c1")
    assert [e["n"] for e in chain] == [1, 3]


def test_load_audit_skips_blank_and_garbage(tmp_path):
    p = tmp_path / "a.log"
    p.write_text('{"x":1}\n\nnot json\n{"y":2}\n')
    out = load_audit(p)
    assert out == [{"x": 1}, {"y": 2}]


# --- end-to-end through Core ---------------------------------------------

async def test_mesh_db_serves_count_via_mesh(mesh_db_core):
    url = mesh_db_core["url"]
    audit_path = mesh_db_core["audit_path"]

    db = MeshNode(node_id="mesh_db_node",
                  secret=os.environ["MESH_DB_NODE_SECRET"], core_url=url)
    await db.connect()
    for name, h in make_handlers(audit_path).items():
        db.on(name, h)
    await db.serve()

    actor = MeshNode(node_id="demo_actor",
                     secret=os.environ["DEMO_ACTOR_SECRET"], core_url=url)
    await actor.start()

    try:
        # Generate some routed traffic.
        for i in range(3):
            r = await actor.invoke("mesh_db_node.ping", {"i": i})
            assert r["kind"] == "response"
            assert r["payload"]["pong"] == {"i": i}

        # Now ask mesh_db itself for counts. This invocation also lands in audit.log.
        result = await actor.invoke("mesh_db_node.count", {"group_by": "decision"})
        assert result["kind"] == "response"
        counts = result["payload"]["counts"]
        # Expect at least: 3 ping invocations + 3 ping responses + this count invocation = 7 routed.
        assert counts.get("routed", 0) >= 7
    finally:
        await asyncio.gather(db.stop(), actor.stop())


async def test_mesh_db_traces_correlation_chain(mesh_db_core):
    url = mesh_db_core["url"]
    audit_path = mesh_db_core["audit_path"]

    db = MeshNode(node_id="mesh_db_node",
                  secret=os.environ["MESH_DB_NODE_SECRET"], core_url=url)
    await db.connect()
    for name, h in make_handlers(audit_path).items():
        db.on(name, h)
    await db.serve()

    actor = MeshNode(node_id="demo_actor",
                     secret=os.environ["DEMO_ACTOR_SECRET"], core_url=url)
    await actor.start()

    try:
        ping_resp = await actor.invoke("mesh_db_node.ping", {"hello": "trace-me"})
        cid = ping_resp["correlation_id"]
        assert cid

        trace = await actor.invoke("mesh_db_node.trace", {"correlation_id": cid})
        chain = trace["payload"]["chain"]
        # Original invocation + response = at least 2 entries on this correlation_id.
        kinds = sorted({e["type"] for e in chain})
        assert "invocation" in kinds
        assert "response" in kinds
        assert all(e["correlation_id"] == cid for e in chain)
    finally:
        await asyncio.gather(db.stop(), actor.stop())


async def test_mesh_db_query_filters_by_to_surface(mesh_db_core):
    url = mesh_db_core["url"]
    audit_path = mesh_db_core["audit_path"]

    db = MeshNode(node_id="mesh_db_node",
                  secret=os.environ["MESH_DB_NODE_SECRET"], core_url=url)
    await db.connect()
    for name, h in make_handlers(audit_path).items():
        db.on(name, h)
    await db.serve()

    actor = MeshNode(node_id="demo_actor",
                     secret=os.environ["DEMO_ACTOR_SECRET"], core_url=url)
    await actor.start()

    try:
        for _ in range(2):
            await actor.invoke("mesh_db_node.ping", {})
        result = await actor.invoke("mesh_db_node.query",
                                     {"where": {"to_surface": "mesh_db_node.ping",
                                                "type": "invocation"}})
        matched = result["payload"]["matched"]
        assert len(matched) == 2
        assert all(m["to_surface"] == "mesh_db_node.ping" for m in matched)
        assert all(m["type"] == "invocation" for m in matched)
    finally:
        await asyncio.gather(db.stop(), actor.stop())
