"""Tests for voice_actor — surface registration, schemas, graceful degradation.

These tests deliberately do NOT exercise the OpenAI Realtime API — that
costs money and needs the network. We assert that:
    * The node registers cleanly even when OPENAI_API_KEY is missing.
    * All five tool surfaces (start_session, stop_session, say,
      session_status, ui_visibility) are visible to Core.
    * Their schemas validate sample payloads correctly.
    * Tool calls without a key return the documented error envelope.
    * Tool calls that need a session before there is one fail gracefully.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import pathlib
import socket

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from core.core import make_app  # noqa: E402
from node_sdk import MeshNode  # noqa: E402

VOICE_MANIFEST = ROOT / "manifests" / "voice_actor_demo.yaml"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _set_secrets() -> None:
    for nid in ("voice_actor", "nexus_agent", "webui_node", "human_node"):
        var = f"{nid.upper()}_SECRET"
        os.environ.setdefault(var, hashlib.sha256(f"mesh:{nid}:test".encode()).hexdigest())
    # Make sure tests run with no API key — confirms graceful degradation.
    os.environ.pop("OPENAI_API_KEY", None)


@pytest_asyncio.fixture
async def voice_core(tmp_path):
    _set_secrets()
    audit_path = tmp_path / "audit.log"
    os.environ["AUDIT_LOG"] = str(audit_path)
    app = make_app(str(VOICE_MANIFEST), str(audit_path))
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    yield {"url": f"http://127.0.0.1:{port}", "state": app["state"]}
    await runner.cleanup()


@pytest_asyncio.fixture
async def voice_actor_node(voice_core, monkeypatch):
    """Spawn the voice_actor mesh node WITHOUT touching real audio devices.

    We monkeypatch MicCapture/SpeakerPlayback/check_devices so the test can
    run on headless CI machines.
    """
    from nodes.voice_actor import voice_actor as va_mod

    class _FakeMic:
        def __init__(self, *a, **kw): self.last_rms = 0.0
        def start(self): raise va_mod.AudioUnavailable("no mic in tests")
        async def stop(self): pass
        async def get(self): await asyncio.sleep(3600); return b""

    class _FakeSpk:
        def __init__(self, *a, **kw): pass
        def start(self): raise va_mod.AudioUnavailable("no speaker in tests")
        async def stop(self): pass
        def play(self, _): pass
        def clear(self): pass
        @property
        def is_speaking(self): return False

    monkeypatch.setattr(va_mod, "MicCapture", _FakeMic)
    monkeypatch.setattr(va_mod, "SpeakerPlayback", _FakeSpk)
    monkeypatch.setattr(va_mod, "check_devices",
                        lambda: {"input_ok": False, "output_ok": False, "error": "tests"})

    node = MeshNode(node_id="voice_actor", secret=os.environ["VOICE_ACTOR_SECRET"],
                    core_url=voice_core["url"])
    va = va_mod.VoiceActor(node, api_key=None, model="gpt-realtime-2")
    node.on("start_session", va.start_session)
    node.on("stop_session", va.stop_session)
    node.on("say", va.say)
    node.on("session_status", va.session_status)
    await node.start()
    try:
        yield {"node": node, "va": va, "core_url": voice_core["url"]}
    finally:
        await node.stop()


async def _spawn_actor(core_url: str, node_id: str) -> MeshNode:
    node = MeshNode(node_id=node_id, secret=os.environ[f"{node_id.upper()}_SECRET"],
                    core_url=core_url)
    await node.start()
    return node


# ---------- tests ----------

async def test_node_registers_with_all_surfaces(voice_actor_node):
    node = voice_actor_node["node"]
    names = {s["name"] for s in node.surfaces}
    assert names == {"start_session", "stop_session", "say",
                     "session_status", "ui_visibility"}


async def test_admin_state_exposes_voice_schemas(voice_core, voice_actor_node):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{voice_core['url']}/v0/admin/state",
                         headers={"X-Admin-Token": "admin-dev-token"}) as r:
            assert r.status == 200
            full = await r.json()
    voice = next(n for n in full["nodes"] if n["id"] == "voice_actor")
    surfaces = {s["name"]: s for s in voice["surfaces"]}
    # All five surfaces present, each has a non-empty schema.
    for name in ("start_session", "stop_session", "say", "session_status", "ui_visibility"):
        assert name in surfaces, f"missing surface {name}"
        assert surfaces[name].get("schema"), f"{name} has no schema"
    # start_session schema accepts an optional voice + system_prompt.
    assert "voice" in surfaces["start_session"]["schema"]["properties"]
    assert "text" in surfaces["say"]["schema"]["properties"]


async def test_start_session_without_key_returns_clear_error(voice_actor_node):
    nexus = await _spawn_actor(voice_actor_node["core_url"], "nexus_agent")
    try:
        result = await nexus.invoke("voice_actor.start_session",
                                    {"voice": "alloy",
                                     "on_user_transcript_node": "nexus_agent"})
        assert result["kind"] == "response"
        assert result["payload"].get("error") == "openai_key_missing"
        assert "OPENAI_API_KEY" in result["payload"].get("detail", "")
    finally:
        await nexus.stop()


async def test_say_without_key_returns_clear_error(voice_actor_node):
    nexus = await _spawn_actor(voice_actor_node["core_url"], "nexus_agent")
    try:
        result = await nexus.invoke("voice_actor.say", {"text": "hi"})
        assert result["payload"].get("error") == "openai_key_missing"
    finally:
        await nexus.stop()


async def test_say_without_active_session_errors(voice_actor_node):
    # Pretend the key IS present, but no session is open.
    voice_actor_node["va"].api_key = "sk-fake-test"
    nexus = await _spawn_actor(voice_actor_node["core_url"], "nexus_agent")
    try:
        result = await nexus.invoke("voice_actor.say", {"text": "hello"})
        assert result["payload"].get("error") == "no_active_session"
    finally:
        await nexus.stop()


async def test_session_status_when_idle(voice_actor_node):
    nexus = await _spawn_actor(voice_actor_node["core_url"], "nexus_agent")
    try:
        result = await nexus.invoke("voice_actor.session_status", {})
        body = result["payload"]
        assert body["active"] is False
        assert body["key_present"] is False
        assert body["status"] == "idle"
        assert body["session_id"] is None
    finally:
        await nexus.stop()


async def test_stop_session_when_no_session(voice_actor_node):
    nexus = await _spawn_actor(voice_actor_node["core_url"], "nexus_agent")
    try:
        result = await nexus.invoke("voice_actor.stop_session", {})
        body = result["payload"]
        assert body["stopped"] is False
        assert body["reason"] == "no_active_session"
    finally:
        await nexus.stop()


async def test_realtime_client_decode_audio_delta_roundtrip():
    """realtime_client.decode_audio_delta is a pure function — can test offline."""
    import base64
    from nodes.voice_actor.realtime_client import decode_audio_delta
    raw = b"\x01\x02\x03\x04hello world"
    b64 = base64.b64encode(raw).decode("ascii")
    assert decode_audio_delta(b64) == raw


async def test_say_schema_rejects_empty_text(voice_actor_node):
    """Core's JSON-schema validation should reject voice_actor.say with empty text."""
    nexus = await _spawn_actor(voice_actor_node["core_url"], "nexus_agent")
    try:
        from node_sdk import MeshError
        try:
            await nexus.invoke("voice_actor.say", {"text": ""})
            raised = False
        except MeshError:
            raised = True
        assert raised, "expected schema validation to reject empty text"
    finally:
        await nexus.stop()
