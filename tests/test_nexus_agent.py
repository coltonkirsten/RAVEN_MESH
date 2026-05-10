"""Tests for the nexus_agent node.

Coverage:
  - mcp_bridge tool definitions are well-formed (names, schemas).
  - The agent registers with Core and exposes its surfaces.
  - The inbox handler logs incoming messages to data/logs/.
  - The control HTTP server's tool endpoints work end-to-end (in particular
    mesh_invoke routes through the live MeshNode SDK).

Real claude subprocess invocations are MOCKED — we replace
``nodes.nexus_agent.cli_runner.run_claude`` with an async stub. This keeps
the test suite hermetic and fast.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import socket
import sys
import uuid

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.core import make_app  # noqa: E402
from node_sdk import MeshNode  # noqa: E402

from nodes.nexus_agent import agent as agent_mod  # noqa: E402
from nodes.nexus_agent import cli_runner as cli_runner_mod  # noqa: E402
from nodes.nexus_agent import mcp_bridge  # noqa: E402
from nodes.nexus_agent.cli_runner import CliResult  # noqa: E402
from nodes.nexus_agent.web.server import AgentInspectorState, make_inspector_app  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _derive(node_id: str) -> str:
    return hashlib.sha256(f"mesh:{node_id}:dev".encode()).hexdigest()


@pytest_asyncio.fixture
async def core_with_agent_manifest(tmp_path):
    # Set deterministic secrets matching what scripts/_env.sh derives.
    os.environ.setdefault("NEXUS_AGENT_SECRET", _derive("nexus_agent"))
    os.environ.setdefault("WEBUI_NODE_SECRET", _derive("webui_node"))
    os.environ.setdefault("CRON_NODE_SECRET", _derive("cron_node"))
    os.environ.setdefault("APPROVAL_NODE_SECRET", _derive("approval_node"))
    os.environ.setdefault("HUMAN_NODE_SECRET", _derive("human_node"))
    audit_path = tmp_path / "audit.log"
    os.environ["AUDIT_LOG"] = str(audit_path)
    manifest = ROOT / "manifests" / "nexus_agent_demo.yaml"
    app = make_app(str(manifest), str(audit_path))
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    yield {"url": f"http://127.0.0.1:{port}", "state": app["state"]}
    await runner.cleanup()


# ---------- mcp_bridge tool definitions ----------

def test_bridge_tools_are_wellformed():
    names = [t.name for t in mcp_bridge.TOOLS]
    expected = {
        "mesh_list_surfaces", "mesh_invoke", "mesh_send_to_inbox",
        "memory_read", "memory_write", "list_skills", "read_skill",
    }
    assert expected.issubset(set(names)), f"missing: {expected - set(names)}"
    for t in mcp_bridge.TOOLS:
        assert t.name and t.description and isinstance(t.inputSchema, dict)
        assert t.inputSchema.get("type") == "object"


# ---------- agent registers + has surfaces ----------

@pytest.mark.asyncio
async def test_agent_registers_and_exposes_surfaces(core_with_agent_manifest):
    base = core_with_agent_manifest["url"]
    secret = os.environ["NEXUS_AGENT_SECRET"]
    node = MeshNode(node_id="nexus_agent", secret=secret, core_url=base)
    await node.connect()
    surface_names = {s["name"] for s in node.surfaces}
    assert {"inbox", "status", "ui_visibility"}.issubset(surface_names)
    # outbound edges include webui_node + cron_node + self
    edges = {(r["from"], r["to"]) for r in node.relationships}
    assert ("nexus_agent", "webui_node.show_message") in edges
    assert ("nexus_agent", "nexus_agent.inbox") in edges
    await node.stop()


# ---------- inbox handler logs the message ----------

@pytest.mark.asyncio
async def test_inbox_handler_logs_incoming_and_invokes_runner(core_with_agent_manifest, tmp_path, monkeypatch):
    """Drive the agent end-to-end with run_claude mocked. Confirm the inbox
    handler logs the incoming envelope, invokes the (mocked) runner, and
    publishes events onto the inspector bus."""
    base = core_with_agent_manifest["url"]

    # Redirect logs to tmp so we can assert on them without polluting the repo.
    log_dir = tmp_path / "logs"
    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(agent_mod, "LOGS_DIR", log_dir)
    monkeypatch.setattr(agent_mod, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(agent_mod, "SESSION_FILE", sessions_dir / "current.json")

    # Stub the claude subprocess.
    captured = {}

    async def fake_run_claude(**kwargs):
        captured["message"] = kwargs["message"]
        captured["system_prompt_len"] = len(kwargs["system_prompt"])
        await kwargs["on_event"]("agent_message", {"type": "system", "subtype": "init", "session_id": "test-sess"})
        await kwargs["on_event"]("agent_message", {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hi from mock"}]},
        })
        await kwargs["on_event"]("agent_message", {
            "type": "result",
            "result": "hi from mock",
            "duration_ms": 5,
            "usage": {"input_tokens": 10, "output_tokens": 4},
        })
        return CliResult(
            result_text="hi from mock",
            session_id="test-sess",
            input_tokens=10, output_tokens=4,
            exit_code=0,
        )

    monkeypatch.setattr(agent_mod, "run_claude", fake_run_claude)

    # Build the runtime + node.
    inspector = AgentInspectorState(
        node_id="nexus_agent",
        ledger_dir=agent_mod.LEDGER_DIR,
        skills_dir=agent_mod.SKILLS_DIR,
        logs_dir=log_dir,
        control_port=12345,
        model="mock-model",
    )
    secret = os.environ["NEXUS_AGENT_SECRET"]
    node = MeshNode(node_id="nexus_agent", secret=secret, core_url=base)
    rt = agent_mod.AgentRuntime(node=node, model="mock-model", inspector=inspector)
    inspector.runtime = rt
    node.on("inbox", rt.handle_inbox)
    await node.start()

    # Forge a fire_and_forget envelope and pass it directly to the handler.
    env = {
        "id": str(uuid.uuid4()),
        "from": "human_node",
        "to": "nexus_agent.inbox",
        "kind": "invocation",
        "payload": {"text": "ping"},
        "timestamp": "2026-05-09T00:00:00Z",
    }
    result = await rt.handle_inbox(env)

    assert captured["message"] == "ping"
    assert captured["system_prompt_len"] > 0
    assert result["ok"] is True
    assert result["text"] == "hi from mock"
    assert rt.session_id == "test-sess"
    assert rt.run_count == 1

    # Logs were written.
    log_files = list(log_dir.glob("*.json"))
    assert any("incoming" in p.name for p in log_files)
    assert any("result" in p.name for p in log_files)

    # Inspector saw a user_message event and a run_done event.
    kinds = [evt["kind"] for evt in inspector.history]
    assert "user_message" in kinds
    assert "run_done" in kinds

    await node.stop()


# ---------- control server invoke + memory ----------

@pytest_asyncio.fixture
async def running_agent(core_with_agent_manifest, tmp_path, monkeypatch):
    """Boot the agent's control + inspector servers (no claude actually
    spawned). Yields URLs and a token for direct probing."""
    base = core_with_agent_manifest["url"]
    log_dir = tmp_path / "logs"
    sessions_dir = tmp_path / "sessions"
    ledger_dir = tmp_path / "ledger"
    skills_dir = ledger_dir / "skills"
    skills_dir.mkdir(parents=True)
    (ledger_dir / "identity.md").write_text("test identity")
    (ledger_dir / "memory.md").write_text("initial memory")
    (skills_dir / "ping.md").write_text("# ping\n\nA test skill.")

    monkeypatch.setattr(agent_mod, "LOGS_DIR", log_dir)
    monkeypatch.setattr(agent_mod, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(agent_mod, "SESSION_FILE", sessions_dir / "current.json")
    monkeypatch.setattr(agent_mod, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(agent_mod, "SKILLS_DIR", skills_dir)

    # Boot a webui_node that can echo back, so mesh_invoke succeeds.
    webui = MeshNode(node_id="webui_node", secret=os.environ["WEBUI_NODE_SECRET"], core_url=base)
    received: list[dict] = []

    async def on_show(env):
        received.append(env["payload"])
        return {"ok": True, "echoed": env["payload"]}

    webui.on("show_message", on_show)
    webui.on("change_color", on_show)
    await webui.start()

    inspector = AgentInspectorState(
        node_id="nexus_agent",
        ledger_dir=ledger_dir,
        skills_dir=skills_dir,
        logs_dir=log_dir,
        control_port=_free_port(),
        model="mock-model",
    )
    secret = os.environ["NEXUS_AGENT_SECRET"]
    node = MeshNode(node_id="nexus_agent", secret=secret, core_url=base)
    rt = agent_mod.AgentRuntime(node=node, model="mock-model", inspector=inspector)
    inspector.runtime = rt
    inspector.control_port = _free_port()  # rebind to a fresh free port
    await node.start()

    control_app = agent_mod.make_control_app(rt)
    control_runner = web.AppRunner(control_app)
    await control_runner.setup()
    control_port = inspector.control_port
    control_site = web.TCPSite(control_runner, "127.0.0.1", control_port)
    await control_site.start()

    yield {
        "control_url": f"http://127.0.0.1:{control_port}",
        "control_token": rt.control_token,
        "rt": rt,
        "received": received,
    }

    await control_runner.cleanup()
    await node.stop()
    await webui.stop()


@pytest.mark.asyncio
async def test_control_mesh_invoke_routes_through_sdk(running_agent):
    headers = {"X-Control-Token": running_agent["control_token"]}
    body = {"target_surface": "webui_node.show_message", "payload": {"text": "yo"}}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.post(f"{running_agent['control_url']}/invoke", json=body) as r:
            data = await r.json()
    assert r.status == 200
    # The webui stub echoes — so the response payload should include it.
    payload = data.get("payload") or data
    assert payload.get("ok") is True
    assert running_agent["received"] == [{"text": "yo"}]


@pytest.mark.asyncio
async def test_control_memory_read_and_write(running_agent):
    headers = {"X-Control-Token": running_agent["control_token"]}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(f"{running_agent['control_url']}/memory") as r:
            initial = await r.json()
        assert initial["content"] == "initial memory"

        async with s.post(f"{running_agent['control_url']}/memory",
                          json={"content": "rewritten", "mode": "replace"}) as r:
            assert r.status == 200

        async with s.get(f"{running_agent['control_url']}/memory") as r:
            after = await r.json()
        assert after["content"] == "rewritten"


@pytest.mark.asyncio
async def test_control_skills_listing_and_read(running_agent):
    headers = {"X-Control-Token": running_agent["control_token"]}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(f"{running_agent['control_url']}/skills") as r:
            data = await r.json()
        assert "ping.md" in data["skills"]

        async with s.get(f"{running_agent['control_url']}/skills/ping") as r:
            sk = await r.json()
        assert "ping" in sk["content"]


@pytest.mark.asyncio
async def test_control_rejects_unauthed(running_agent):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{running_agent['control_url']}/memory") as r:
            assert r.status == 403


@pytest.mark.asyncio
async def test_control_surfaces_lists_outgoing_edges(running_agent):
    headers = {"X-Control-Token": running_agent["control_token"]}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(f"{running_agent['control_url']}/surfaces") as r:
            data = await r.json()
    targets = {s["target"] for s in data["surfaces"]}
    assert "webui_node.show_message" in targets
    assert "cron_node.set" in targets
