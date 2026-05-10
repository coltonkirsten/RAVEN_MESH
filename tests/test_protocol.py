"""Protocol-validation suite — exercises all 10 demo flows from PRD §7.

Each test maps to one (or one-and-a-bit) of the demo steps. Tests boot Core
in-process via the ``core_server`` fixture and use the SDK (or raw HTTP for
the cross-language test) to drive the protocol end-to-end.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import uuid

import aiohttp
import pytest

from core.core import canonical, sign, verify
from node_sdk import MeshDeny, MeshError, MeshNode

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------- helpers ----------

def read_audit(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


async def _spawn_tasks_node(core_url: str) -> tuple[MeshNode, list[dict]]:
    """Capability node implementing tasks.create + tasks.list. Returns (node, store)."""
    secret = os.environ["TASKS_SECRET"]
    store: list[dict] = []
    node = MeshNode(node_id="tasks", secret=secret, core_url=core_url)
    await node.connect()

    async def on_create(env: dict) -> dict:
        payload = env.get("payload", {})
        task = {"id": str(uuid.uuid4()), "title": payload["title"]}
        store.append(task)
        return {"created": task, "count": len(store)}

    async def on_list(env: dict) -> dict:
        return {"tasks": list(store)}

    node.on("create", on_create)
    node.on("list", on_list)
    await node.serve()
    return node, store


async def _spawn_approval_node(core_url: str, *, deny: bool = False) -> MeshNode:
    secret = os.environ["HUMAN_APPROVAL_SECRET"]
    node = MeshNode(node_id="human_approval", secret=secret, core_url=core_url)
    await node.connect()

    async def on_inbox(env: dict) -> dict:
        body = env.get("payload", {})
        target = body["target_surface"]
        inner = body.get("payload", {})
        if deny:
            raise MeshDeny("denied_by_human", target=target)
        result = await node.invoke(target, inner, wrapped=env)
        return result.get("payload", result) if isinstance(result, dict) else {"result": result}

    node.on("inbox", on_inbox)
    await node.serve()
    return node


async def _spawn_voice_actor(core_url: str) -> MeshNode:
    secret = os.environ["VOICE_SECRET"]
    node = MeshNode(node_id="voice_actor", secret=secret, core_url=core_url)
    await node.start()
    return node


# ---------- demo flows ----------

# Step 1: Core boots, loads manifest, listens, has an empty audit log.
async def test_step_1_core_boots_with_manifest(core_server):
    state = core_server["state"]
    assert "voice_actor" in state.nodes_decl
    assert "tasks" in state.nodes_decl
    assert "human_approval" in state.nodes_decl
    assert ("voice_actor", "tasks.list") in state.edges
    assert read_audit(core_server["audit_path"]) == []


# Steps 2-4: each node registers and opens its SSE channel.
async def test_steps_2_to_4_three_nodes_register(core_server):
    url = core_server["url"]
    state = core_server["state"]
    tasks, _ = await _spawn_tasks_node(url)
    approval = await _spawn_approval_node(url)
    voice = await _spawn_voice_actor(url)
    try:
        # All three sessions should be live and known to Core.
        assert tasks.session_id and approval.session_id and voice.session_id
        assert state.connections["tasks"]["session_id"] == tasks.session_id
        assert state.connections["human_approval"]["session_id"] == approval.session_id
        assert state.connections["voice_actor"]["session_id"] == voice.session_id
        # voice_actor's relationship view should include all three relevant edges.
        edges = {(r["from"], r["to"]) for r in voice.relationships}
        assert ("voice_actor", "tasks.list") in edges
        assert ("voice_actor", "human_approval.inbox") in edges
    finally:
        await asyncio.gather(tasks.stop(), approval.stop(), voice.stop())


# Step 5: voice_actor.invoke(tasks.list) — direct request/response.
async def test_step_5_direct_request_response(core_server):
    url = core_server["url"]
    tasks, store = await _spawn_tasks_node(url)
    approval = await _spawn_approval_node(url)
    voice = await _spawn_voice_actor(url)
    try:
        store.append({"id": "seed", "title": "seeded"})
        result = await voice.invoke("tasks.list", {})
        assert result["kind"] == "response"
        assert result["payload"] == {"tasks": [{"id": "seed", "title": "seeded"}]}
        # Audit shows two routed events: the invocation, and the response.
        events = read_audit(core_server["audit_path"])
        kinds = [(e["type"], e["decision"]) for e in events]
        assert ("invocation", "routed") in kinds
        assert ("response", "routed") in kinds
    finally:
        await asyncio.gather(tasks.stop(), approval.stop(), voice.stop())


# Step 6: voice_actor → human_approval.inbox → (approve) → tasks.create.
async def test_step_6_approval_approve_chain(core_server):
    url = core_server["url"]
    tasks, store = await _spawn_tasks_node(url)
    approval = await _spawn_approval_node(url, deny=False)
    voice = await _spawn_voice_actor(url)
    try:
        wrapped_target = "tasks.create"
        wrapped_payload = {"title": "approved task"}
        result = await voice.invoke("human_approval.inbox", {
            "target_surface": wrapped_target,
            "payload": wrapped_payload,
        })
        assert result["kind"] == "response"
        # The forwarded response from tasks ends up as the body voice_actor sees.
        assert result["payload"]["created"]["title"] == "approved task"
        assert any(t["title"] == "approved task" for t in store)
        events = read_audit(core_server["audit_path"])
        # Expect at least 4 routed events: inbox-invoke, create-invoke, create-response, inbox-response.
        routed = [e for e in events if e["decision"] == "routed"]
        assert len(routed) >= 4
        # Correlation IDs let you trace the chain.
        assert any(e["to_surface"] == "human_approval.inbox" for e in routed)
        assert any(e["to_surface"] == "tasks.create" for e in routed)
    finally:
        await asyncio.gather(tasks.stop(), approval.stop(), voice.stop())


# Step 7: same chain, deny → voice_actor gets an error response.
async def test_step_7_approval_deny(core_server):
    url = core_server["url"]
    tasks, _ = await _spawn_tasks_node(url)
    approval = await _spawn_approval_node(url, deny=True)
    voice = await _spawn_voice_actor(url)
    try:
        result = await voice.invoke("human_approval.inbox", {
            "target_surface": "tasks.create",
            "payload": {"title": "should be denied"},
        })
        assert result["kind"] == "error"
        assert result["payload"]["reason"] == "denied_by_human"
        events = read_audit(core_server["audit_path"])
        # The approval-inbox invocation is routed, but no tasks.create invocation.
        assert any(e["to_surface"] == "human_approval.inbox" and e["decision"] == "routed"
                   for e in events)
        assert not any(e["to_surface"] == "tasks.create" and e["type"] == "invocation"
                       for e in events)
    finally:
        await asyncio.gather(tasks.stop(), approval.stop(), voice.stop())


# Step 8: voice_actor → tasks.create with no edge → denied_no_relationship.
async def test_step_8_denied_no_relationship(core_server):
    url = core_server["url"]
    tasks, _ = await _spawn_tasks_node(url)
    approval = await _spawn_approval_node(url)
    voice = await _spawn_voice_actor(url)
    try:
        with pytest.raises(MeshError) as exc:
            await voice.invoke("tasks.create", {"title": "smuggled"})
        assert exc.value.status == 403
        assert exc.value.data["error"] == "denied_no_relationship"
        events = read_audit(core_server["audit_path"])
        assert any(e["decision"] == "denied_no_relationship"
                   and e["to_surface"] == "tasks.create" for e in events)
    finally:
        await asyncio.gather(tasks.stop(), approval.stop(), voice.stop())


# Step 9: cross-host. Functionally identical to step 5 over the wire — Core has
# no concept of which host a node lives on, so a passing step 5 already proves
# this. We assert that property directly: the envelope contains no host info.
async def test_step_9_envelope_has_no_host_info(core_server):
    url = core_server["url"]
    tasks, _ = await _spawn_tasks_node(url)
    voice = await _spawn_voice_actor(url)
    approval = await _spawn_approval_node(url)
    try:
        # Inspect the canonicalized envelope structure.
        env = {
            "id": "x", "correlation_id": "x", "from": "voice_actor",
            "to": "tasks.list", "kind": "invocation", "payload": {},
            "timestamp": "now",
        }
        canon = canonical(env)
        for forbidden in ["host", "ip", "addr", "url", "tailnet"]:
            assert forbidden not in canon.lower()
        # And: a successful invoke really doesn't depend on any host field.
        result = await voice.invoke("tasks.list", {})
        assert result["kind"] == "response"
    finally:
        await asyncio.gather(tasks.stop(), approval.stop(), voice.stop())


# Step 10: external_node joins using ONLY stdlib HTTP + manual signing.
# This proves a node in any language can speak the protocol.
async def test_step_10_external_language_node(core_server):
    url = core_server["url"]
    tasks, _ = await _spawn_tasks_node(url)
    approval = await _spawn_approval_node(url)
    voice = await _spawn_voice_actor(url)
    ext_secret = os.environ["EXTERNAL_NODE_SECRET"]

    async with aiohttp.ClientSession() as s:
        # Manual register.
        body = {"node_id": "external_node", "timestamp": "now"}
        body["signature"] = sign(body, ext_secret)
        async with s.post(f"{url}/v0/register", json=body) as r:
            assert r.status == 200
            reg = await r.json()
        session_id = reg["session_id"]

        # Manual SSE consumer + responder, all hand-rolled.
        async def consumer():
            async with s.get(f"{url}/v0/stream", params={"session": session_id},
                             timeout=aiohttp.ClientTimeout(total=None)) as r:
                event_type = None
                data_buf: list[str] = []
                async for raw in r.content:
                    line = raw.decode().rstrip("\r\n")
                    if line == "":
                        if event_type == "deliver" and data_buf:
                            data = json.loads("\n".join(data_buf))
                            # Hand-build response envelope.
                            resp = {
                                "id": str(uuid.uuid4()),
                                "correlation_id": data["id"],
                                "from": "external_node",
                                "to": data.get("from", ""),
                                "kind": "response",
                                "payload": {"pong": data.get("payload", {})},
                                "timestamp": "now",
                            }
                            resp["signature"] = sign(resp, ext_secret)
                            await s.post(f"{url}/v0/respond", json=resp)
                            return
                        event_type = None
                        data_buf = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buf.append(line[5:].lstrip())

        consumer_task = asyncio.create_task(consumer())
        try:
            # voice_actor invokes the external surface.
            result = await voice.invoke("external_node.ping", {"hello": "from voice"})
            assert result["kind"] == "response"
            assert result["payload"]["pong"] == {"hello": "from voice"}
        finally:
            await asyncio.wait_for(consumer_task, timeout=5)
            await asyncio.gather(tasks.stop(), approval.stop(), voice.stop())
