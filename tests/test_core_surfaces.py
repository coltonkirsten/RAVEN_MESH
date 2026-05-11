"""End-to-end tests for the eleven ``core.*`` surfaces (SPEC §5).

These tests exercise the in-process dispatch path: an envelope addressed to
``core.<name>`` travels /v0/invoke through the normal HMAC + replay + edge +
schema gauntlet, then is handled by the built-in ``core`` node. Responses
come back as proper signed envelopes ``from: "core"``.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib

import pytest
import yaml

from node_sdk import MeshNode

from tests.conftest import TEST_ADMIN_TOKEN as ADMIN_TOKEN  # noqa: F401

OPERATOR = "voice_actor"  # the demo manifest grants voice_actor every core.* edge


@pytest.fixture
async def operator(core_server):
    node = MeshNode(node_id=OPERATOR, secret=os.environ["VOICE_SECRET"],
                      core_url=core_server["url"])
    await node.connect()
    yield node
    await node.stop()


async def _payload(operator: MeshNode, surface: str, payload: dict | None = None) -> dict:
    env = await operator.invoke(f"core.{surface}", payload or {})
    assert env["kind"] == "response", env
    assert env["from"] == "core"
    return env["payload"]


# ---------- 11 surfaces ----------

async def test_core_state(operator, core_server):
    body = await _payload(operator, "state")
    # manifest_path is the ephemeral test manifest constructed by the fixture.
    assert body["manifest_path"] == str(core_server["manifest_path"])
    node_ids = {n["id"] for n in body["nodes"]}
    assert "core" in node_ids
    assert {"voice_actor", "tasks", "human_approval"} <= node_ids
    core_node = next(n for n in body["nodes"] if n["id"] == "core")
    assert core_node["runtime"] == "in-process"
    surface_names = {s["name"] for s in core_node["surfaces"]}
    assert surface_names == {
        "state", "processes", "metrics", "audit_query",
        "set_manifest", "reload_manifest",
        "spawn", "stop", "restart", "reconcile", "drain",
    }
    # Each surface ships its full schema dict.
    for s in core_node["surfaces"]:
        assert s["schema"]["type"] == "object"


async def test_core_processes_supervisor_disabled(operator):
    body = await _payload(operator, "processes")
    assert body == {"supervisor_enabled": False, "processes": []}


async def test_core_metrics(operator, core_server):
    body = await _payload(operator, "metrics")
    assert body["nodes_declared"] >= 4
    assert body["edges"] >= 11   # the demo manifest grants 11 core edges
    assert body["pending"] >= 0
    assert body["replay_nonce_lru"] >= 1   # at least the registration / this call
    assert body["supervisor"] is None


async def test_core_audit_query_default(operator, core_server):
    # Make at least one routed event to find.
    await operator.invoke("core.state", {})
    body = await _payload(operator, "audit_query", {"last_n": 50})
    assert isinstance(body["results"], list)
    assert body["results"]   # non-empty
    # Most recent first.
    timestamps = [r.get("timestamp") for r in body["results"]]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_core_audit_query_filters(operator, core_server):
    await operator.invoke("core.state", {})
    body = await _payload(operator, "audit_query",
                           {"to_surface": "core.state", "decision": "routed"})
    assert all(r.get("to_surface") == "core.state" for r in body["results"])
    assert all(r.get("decision") == "routed" for r in body["results"])


async def test_core_audit_query_respects_max_last_n(operator):
    """Schema enforces last_n ≤ 1000; the handler then caps actual returns."""
    body = await _payload(operator, "audit_query", {"last_n": 1000})
    assert len(body["results"]) <= 1000
    # Out-of-range values are rejected by jsonschema as denied_schema_invalid
    # (covered indirectly by tests/test_protocol.py replay-window guards).


async def test_core_reload_manifest(operator, core_server):
    body = await _payload(operator, "reload_manifest")
    assert body["ok"] is True
    assert body["nodes_declared"] >= 4
    assert "edges_changed" in body


async def test_core_set_manifest_round_trip(operator, core_server, tmp_path):
    """core.set_manifest writes the YAML, reloads, and reports sanitized totals."""
    manifest_text = pathlib.Path(core_server["state"].manifest_path).read_text()
    parsed = yaml.safe_load(manifest_text)
    # No-op edit: round-trip the YAML with one extra metadata key.
    parsed["nodes"][0].setdefault("metadata", {})["round_trip"] = True
    new_yaml = yaml.safe_dump(parsed)
    body = await _payload(operator, "set_manifest", {"yaml": new_yaml})
    assert body["ok"] is True
    # nodes_declared includes the implicit ``core`` node (SPEC §5.1).
    assert body["nodes_declared"] == len(parsed["nodes"]) + 1


async def test_core_set_manifest_rejects_bad_yaml(operator):
    env = await operator.invoke("core.set_manifest", {"yaml": "not: valid: yaml: ::"})
    assert env["kind"] == "error"
    assert env["payload"]["error"] in ("bad_yaml", "manifest_missing_nodes")


async def test_core_spawn_without_supervisor(operator):
    env = await operator.invoke("core.spawn", {"node_id": "tasks"})
    assert env["kind"] == "error"
    assert env["payload"]["error"] == "supervisor_disabled"


async def test_core_stop_without_supervisor(operator):
    env = await operator.invoke("core.stop", {"node_id": "tasks"})
    assert env["kind"] == "error"
    assert env["payload"]["error"] == "supervisor_disabled"


async def test_core_restart_without_supervisor(operator):
    env = await operator.invoke("core.restart", {"node_id": "tasks"})
    assert env["kind"] == "error"
    assert env["payload"]["error"] == "supervisor_disabled"


async def test_core_reconcile_without_supervisor(operator):
    env = await operator.invoke("core.reconcile", {})
    assert env["kind"] == "error"
    assert env["payload"]["error"] == "supervisor_disabled"


async def test_core_drain_without_supervisor(operator):
    env = await operator.invoke("core.drain", {"node_id": "tasks"})
    assert env["kind"] == "error"
    assert env["payload"]["error"] == "supervisor_disabled"


# ---------- enforcement / housekeeping ----------

async def test_core_introspect_lists_core_node(core_server):
    """SPEC §5.1: 'core' must appear in /v0/introspect even if the YAML omits it."""
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{core_server['url']}/v0/introspect") as r:
            data = await r.json()
    ids = {n["id"] for n in data["nodes"]}
    assert "core" in ids
    core_node = next(n for n in data["nodes"] if n["id"] == "core")
    assert core_node["runtime"] == "in-process"


async def test_core_state_denied_without_edge(core_server):
    """A node with no edge to core.* is rejected with denied_no_relationship."""
    secret = os.environ["TASKS_SECRET"]
    node = MeshNode(node_id="tasks", secret=secret, core_url=core_server["url"])
    await node.connect()
    try:
        from node_sdk import MeshError
        with pytest.raises(MeshError) as exc_info:
            await node.invoke("core.state", {})
        assert exc_info.value.status == 403
        assert exc_info.value.data["error"] == "denied_no_relationship"
    finally:
        await node.stop()


async def test_manifest_reload_closes_removed_node_session(operator, core_server, tmp_path):
    """SPEC §5.4: a node dropped from the manifest gets its session closed."""
    state = core_server["state"]
    # Spin up a tasks node so it has a live session.
    tasks = MeshNode(node_id="tasks", secret=os.environ["TASKS_SECRET"],
                      core_url=core_server["url"])
    await tasks.start()
    assert "tasks" in state.connections
    try:
        # Edit the manifest to drop the tasks node, then ask Core to reload.
        manifest_text = pathlib.Path(state.manifest_path).read_text()
        parsed = yaml.safe_load(manifest_text)
        parsed["nodes"] = [n for n in parsed["nodes"] if n["id"] != "tasks"]
        parsed["relationships"] = [
            r for r in parsed["relationships"]
            if r["from"] != "tasks" and not r["to"].startswith("tasks.")
        ]
        new_yaml = yaml.safe_dump(parsed)
        env = await operator.invoke("core.set_manifest", {"yaml": new_yaml})
        assert env["kind"] == "response"
        body = env["payload"]
        assert "tasks" in body.get("closed_sessions", [])
        # And the session is in fact gone.
        assert "tasks" not in state.connections
    finally:
        await tasks.stop()
        # tmp_path is unique per test — no cross-test poisoning to undo.


async def test_replay_protection_emits_denied_replay(operator, core_server):
    """SPEC §7: a replayed envelope id audits as ``denied_replay``."""
    import hashlib
    import uuid
    import aiohttp
    from core.core import sign, now_iso
    secret = os.environ["VOICE_SECRET"]
    msg_id = str(uuid.uuid4())
    env = {
        "id": msg_id,
        "correlation_id": msg_id,
        "from": "voice_actor",
        "to": "core.state",
        "kind": "invocation",
        "payload": {},
        "timestamp": now_iso(),
    }
    env["signature"] = sign(env, secret)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{core_server['url']}/v0/invoke", json=env) as r:
            assert r.status == 200
        async with s.post(f"{core_server['url']}/v0/invoke", json=env) as r:
            assert r.status == 409
            body = await r.json()
            assert body["error"] == "replay_detected"
    # Audit log records the second attempt under the SPEC §7 code.
    audit_lines = pathlib.Path(core_server["audit_path"]).read_text().splitlines()
    decisions = [json.loads(l).get("decision") for l in audit_lines]
    assert "denied_replay" in decisions
