"""Tests for the nexus_agent_isolated node.

Coverage:
  - mcp_bridge tool definitions are well-formed (names, schemas).
  - The agent registers with Core and exposes its surfaces under the
    nexus_agent_isolated_demo manifest.
  - The inbox handler logs incoming messages and invokes the (mocked)
    docker runner, capturing the args we'd pass to `docker run`.
  - The control HTTP server's tool endpoints work end-to-end (mesh_invoke
    routes through the live MeshNode SDK; memory + skills are served
    correctly).
  - The keychain extractor / auth resolver behaves sanely.

Real `docker run` invocations are MOCKED — we replace
``nodes.nexus_agent_isolated.agent.run_claude_in_container`` with an async
stub. This keeps the test suite hermetic and fast (no docker, no claude).
"""
from __future__ import annotations

import hashlib
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

from nodes.nexus_agent_isolated import agent as agent_mod  # noqa: E402
from nodes.nexus_agent_isolated import docker_runner as runner_mod  # noqa: E402
from nodes.nexus_agent_isolated import mcp_bridge  # noqa: E402
from nodes.nexus_agent_isolated.docker_runner import CliResult  # noqa: E402
from nodes.nexus_agent.web.server import AgentInspectorState  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _derive(node_id: str) -> str:
    return hashlib.sha256(f"mesh:{node_id}:dev".encode()).hexdigest()


@pytest_asyncio.fixture
async def core_with_isolated_manifest(tmp_path):
    os.environ.setdefault("NEXUS_AGENT_ISOLATED_SECRET", _derive("nexus_agent_isolated"))
    os.environ.setdefault("WEBUI_NODE_SECRET", _derive("webui_node"))
    os.environ.setdefault("CRON_NODE_SECRET", _derive("cron_node"))
    os.environ.setdefault("APPROVAL_NODE_SECRET", _derive("approval_node"))
    os.environ.setdefault("HUMAN_NODE_SECRET", _derive("human_node"))
    audit_path = tmp_path / "audit.log"
    os.environ["AUDIT_LOG"] = str(audit_path)
    manifest = ROOT / "manifests" / "nexus_agent_isolated_demo.yaml"
    app = make_app(str(manifest), str(audit_path))
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    yield {"url": f"http://127.0.0.1:{port}", "state": app["state"]}
    await runner.cleanup()


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


def test_resolve_auth_env_prefers_explicit_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token")
    monkeypatch.setattr(runner_mod, "get_oauth_token_from_keychain", lambda: "keychain-token")
    out = runner_mod.resolve_auth_env()
    assert out["CLAUDE_CODE_OAUTH_TOKEN"] == "env-token"


def test_resolve_auth_env_falls_back_to_keychain(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(runner_mod, "get_oauth_token_from_keychain", lambda: "keychain-token")
    out = runner_mod.resolve_auth_env()
    assert out == {"CLAUDE_CODE_OAUTH_TOKEN": "keychain-token"}


def test_resolve_auth_env_includes_anthropic_api_key(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(runner_mod, "get_oauth_token_from_keychain", lambda: None)
    out = runner_mod.resolve_auth_env()
    assert out == {"ANTHROPIC_API_KEY": "sk-ant-test"}


@pytest.mark.asyncio
async def test_agent_registers_and_exposes_surfaces(core_with_isolated_manifest):
    base = core_with_isolated_manifest["url"]
    secret = os.environ["NEXUS_AGENT_ISOLATED_SECRET"]
    node = MeshNode(node_id="nexus_agent_isolated", secret=secret, core_url=base)
    await node.connect()
    surface_names = {s["name"] for s in node.surfaces}
    assert {"inbox", "status", "ui_visibility"}.issubset(surface_names)
    edges = {(r["from"], r["to"]) for r in node.relationships}
    assert ("nexus_agent_isolated", "webui_node.show_message") in edges
    assert ("nexus_agent_isolated", "nexus_agent_isolated.inbox") in edges
    await node.stop()


@pytest.mark.asyncio
async def test_inbox_handler_logs_and_calls_docker_runner(
    core_with_isolated_manifest, tmp_path, monkeypatch,
):
    """Drive the agent end-to-end with run_claude_in_container mocked.
    Confirm the runner is called with image + ledger volume args, the
    incoming envelope is logged, and the inspector emits run_done."""
    base = core_with_isolated_manifest["url"]

    log_dir = tmp_path / "logs"
    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(agent_mod, "LOGS_DIR", log_dir)
    monkeypatch.setattr(agent_mod, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(agent_mod, "SESSION_FILE", sessions_dir / "current.json")

    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        await kwargs["on_event"]("agent_message", {
            "type": "system", "subtype": "init", "session_id": "iso-sess",
        })
        await kwargs["on_event"]("agent_message", {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello from container"}]},
        })
        await kwargs["on_event"]("agent_message", {
            "type": "result",
            "result": "hello from container",
            "duration_ms": 7,
            "usage": {"input_tokens": 11, "output_tokens": 5},
        })
        return CliResult(
            result_text="hello from container",
            session_id="iso-sess",
            input_tokens=11, output_tokens=5,
            exit_code=0,
        )

    monkeypatch.setattr(agent_mod, "run_claude_in_container", fake_run)

    inspector = AgentInspectorState(
        node_id="nexus_agent_isolated",
        ledger_dir=agent_mod.LEDGER_DIR,
        skills_dir=agent_mod.SKILLS_DIR,
        logs_dir=log_dir,
        control_port=12346,
        model="mock-model",
    )
    secret = os.environ["NEXUS_AGENT_ISOLATED_SECRET"]
    node = MeshNode(node_id="nexus_agent_isolated", secret=secret, core_url=base)
    rt = agent_mod.AgentRuntime(
        node=node, model="mock-model", inspector=inspector,
        image="nexus_agent_isolated:test",
        ledger_volume="nexus_agent_isolated_ledger_test",
    )
    inspector.runtime = rt
    node.on("inbox", rt.handle_inbox)
    await node.start()

    env = {
        "id": str(uuid.uuid4()),
        "from": "human_node",
        "to": "nexus_agent_isolated.inbox",
        "kind": "invocation",
        "payload": {"text": "ping isolated"},
        "timestamp": "2026-05-09T00:00:00Z",
    }
    result = await rt.handle_inbox(env)

    assert captured["message"] == "ping isolated"
    assert captured["image"] == "nexus_agent_isolated:test"
    assert captured["ledger_volume"] == "nexus_agent_isolated_ledger_test"
    assert captured["control_port"] == 12346
    assert captured["control_token"] == rt.control_token
    assert captured["system_prompt"]
    assert result["ok"] is True
    assert result["text"] == "hello from container"
    assert rt.session_id == "iso-sess"
    assert rt.run_count == 1

    log_files = list(log_dir.glob("*.json"))
    assert any("incoming" in p.name for p in log_files)
    assert any("result" in p.name for p in log_files)

    kinds = [evt["kind"] for evt in inspector.history]
    assert "user_message" in kinds
    assert "run_done" in kinds

    await node.stop()


@pytest_asyncio.fixture
async def running_isolated_agent(core_with_isolated_manifest, tmp_path, monkeypatch):
    """Boot the isolated agent's control + inspector servers (no docker
    actually spawned). Yields URLs and a token for direct probing."""
    base = core_with_isolated_manifest["url"]
    log_dir = tmp_path / "logs"
    sessions_dir = tmp_path / "sessions"
    ledger_dir = tmp_path / "ledger"
    skills_dir = ledger_dir / "skills"
    skills_dir.mkdir(parents=True)
    (ledger_dir / "identity.md").write_text("test isolated identity")
    (ledger_dir / "memory.md").write_text("isolated memory v1")
    (skills_dir / "ping.md").write_text("# ping\n\nA test skill.")

    monkeypatch.setattr(agent_mod, "LOGS_DIR", log_dir)
    monkeypatch.setattr(agent_mod, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(agent_mod, "SESSION_FILE", sessions_dir / "current.json")
    monkeypatch.setattr(agent_mod, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(agent_mod, "SKILLS_DIR", skills_dir)

    webui = MeshNode(node_id="webui_node", secret=os.environ["WEBUI_NODE_SECRET"], core_url=base)
    received: list[dict] = []

    async def on_show(env):
        received.append(env["payload"])
        return {"ok": True, "echoed": env["payload"]}

    webui.on("show_message", on_show)
    webui.on("change_color", on_show)
    await webui.start()

    inspector = AgentInspectorState(
        node_id="nexus_agent_isolated",
        ledger_dir=ledger_dir,
        skills_dir=skills_dir,
        logs_dir=log_dir,
        control_port=_free_port(),
        model="mock-model",
    )
    secret = os.environ["NEXUS_AGENT_ISOLATED_SECRET"]
    node = MeshNode(node_id="nexus_agent_isolated", secret=secret, core_url=base)
    rt = agent_mod.AgentRuntime(
        node=node, model="mock-model", inspector=inspector,
        image="nexus_agent_isolated:test",
        ledger_volume="nexus_agent_isolated_ledger_test",
    )
    inspector.runtime = rt
    inspector.control_port = _free_port()
    await node.start()

    control_app = agent_mod.make_control_app(rt)
    control_runner = web.AppRunner(control_app)
    await control_runner.setup()
    control_port = inspector.control_port
    # Bind on 127.0.0.1 in tests so we don't expose anything publicly; the
    # production agent uses 0.0.0.0 (so containers can reach it).
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
async def test_control_mesh_invoke_routes_through_sdk(running_isolated_agent):
    headers = {"X-Control-Token": running_isolated_agent["control_token"]}
    body = {"target_surface": "webui_node.show_message", "payload": {"text": "yo"}}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.post(f"{running_isolated_agent['control_url']}/invoke", json=body) as r:
            data = await r.json()
            status = r.status
    assert status == 200
    payload = data.get("payload") or data
    assert payload.get("ok") is True
    assert running_isolated_agent["received"] == [{"text": "yo"}]


@pytest.mark.asyncio
async def test_control_memory_read_and_write(running_isolated_agent):
    headers = {"X-Control-Token": running_isolated_agent["control_token"]}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(f"{running_isolated_agent['control_url']}/memory") as r:
            initial = await r.json()
        assert initial["content"] == "isolated memory v1"

        async with s.post(f"{running_isolated_agent['control_url']}/memory",
                          json={"content": "rewritten by mock", "mode": "replace"}) as r:
            assert r.status == 200

        async with s.get(f"{running_isolated_agent['control_url']}/memory") as r:
            after = await r.json()
        assert after["content"] == "rewritten by mock"


@pytest.mark.asyncio
async def test_control_skills_listing_and_read(running_isolated_agent):
    headers = {"X-Control-Token": running_isolated_agent["control_token"]}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(f"{running_isolated_agent['control_url']}/skills") as r:
            data = await r.json()
        assert "ping.md" in data["skills"]

        async with s.get(f"{running_isolated_agent['control_url']}/skills/ping") as r:
            sk = await r.json()
        assert "ping" in sk["content"]


@pytest.mark.asyncio
async def test_control_rejects_unauthed(running_isolated_agent):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{running_isolated_agent['control_url']}/memory") as r:
            assert r.status == 403


@pytest.mark.asyncio
async def test_control_surfaces_lists_outgoing_edges(running_isolated_agent):
    headers = {"X-Control-Token": running_isolated_agent["control_token"]}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(f"{running_isolated_agent['control_url']}/surfaces") as r:
            data = await r.json()
    targets = {s["target"] for s in data["surfaces"]}
    assert "webui_node.show_message" in targets
    assert "cron_node.set" in targets
