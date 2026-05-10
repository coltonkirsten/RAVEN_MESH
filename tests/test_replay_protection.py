"""Replay-protection tests: timestamp window + nonce LRU on /v0/respond
and /v0/invoke; configurable window via MESH_REPLAY_WINDOW_S."""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import uuid

import aiohttp
import pytest

from core.core import (
    REPLAY_WINDOW_DEFAULT_S,
    REPLAY_WINDOW_MAX_S,
    REPLAY_WINDOW_MIN_S,
    _load_replay_window_s,
    now_iso,
    sign,
)
from node_sdk import MeshNode


# ---------- env-var loader: clamping + fallback ---------------------------

def test_replay_window_default_when_unset(monkeypatch):
    monkeypatch.delenv("MESH_REPLAY_WINDOW_S", raising=False)
    assert _load_replay_window_s() == REPLAY_WINDOW_DEFAULT_S


def test_replay_window_honors_in_range_value(monkeypatch):
    monkeypatch.setenv("MESH_REPLAY_WINDOW_S", "30")
    assert _load_replay_window_s() == 30


def test_replay_window_clamps_above_ceiling(monkeypatch, caplog):
    monkeypatch.setenv("MESH_REPLAY_WINDOW_S", "10000")
    with caplog.at_level("WARNING", logger="mesh.core"):
        val = _load_replay_window_s()
    assert val == REPLAY_WINDOW_MAX_S
    assert any("clamped" in r.message for r in caplog.records)


def test_replay_window_clamps_below_floor(monkeypatch, caplog):
    monkeypatch.setenv("MESH_REPLAY_WINDOW_S", "0")
    with caplog.at_level("WARNING", logger="mesh.core"):
        val = _load_replay_window_s()
    assert val == REPLAY_WINDOW_MIN_S
    assert any("clamped" in r.message for r in caplog.records)


def test_replay_window_falls_back_on_garbage(monkeypatch, caplog):
    monkeypatch.setenv("MESH_REPLAY_WINDOW_S", "not_a_number")
    with caplog.at_level("WARNING", logger="mesh.core"):
        val = _load_replay_window_s()
    assert val == REPLAY_WINDOW_DEFAULT_S
    assert any("not a valid int" in r.message for r in caplog.records)


# ---------- end-to-end gating on /v0/respond ------------------------------

async def _capture_respond_envelope(core_server) -> tuple[str, dict, str]:
    """Drive a real invocation through the mesh and capture the response
    envelope POSTed to /v0/respond, plus the responder's secret.

    Returns ``(core_url, envelope, secret)``.
    """
    core_url = core_server["url"]
    tasks_secret = os.environ["TASKS_SECRET"]
    voice_secret = os.environ["VOICE_SECRET"]

    voice = MeshNode(node_id="voice_actor", secret=voice_secret, core_url=core_url)
    await voice.start()

    captured: dict[str, dict] = {}
    tasks = MeshNode(node_id="tasks", secret=tasks_secret, core_url=core_url)

    original_respond = tasks.respond

    async def capturing_respond(original, payload, *, kind="response"):
        msg_id = str(uuid.uuid4())
        env = {
            "id": msg_id,
            "correlation_id": original.get("id"),
            "from": tasks.node_id,
            "to": original.get("from", ""),
            "kind": kind,
            "payload": payload,
            "timestamp": now_iso(),
        }
        env["signature"] = sign(env, tasks.secret)
        captured["env"] = env
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{core_url}/v0/respond", json=env) as r:
                assert r.status == 200, await r.text()
        return None

    tasks.respond = capturing_respond  # type: ignore[assignment]
    tasks.on("list", lambda env: {"tasks": []})
    await tasks.start()

    try:
        await voice.invoke("tasks.list", {})
        assert "env" in captured, "responder did not POST /v0/respond"
        return core_url, captured["env"], tasks_secret
    finally:
        await asyncio.gather(tasks.stop(), voice.stop())


async def test_respond_rejects_replayed_envelope_within_window(core_server):
    core_url, env, _secret = await _capture_respond_envelope(core_server)
    await asyncio.sleep(0.1)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{core_url}/v0/respond", json=env) as r:
            assert r.status == 409
            data = await r.json()
            assert data["error"] == "replay_detected"


async def test_respond_rejects_envelope_outside_window(core_server, monkeypatch):
    # Make the window tight so we can fake a stale envelope without sleeping.
    state = core_server["state"]
    state.replay_window_s = 5

    core_url = core_server["url"]
    secret = os.environ["TASKS_SECRET"]
    stale = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=70)
    env = {
        "id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "from": "tasks",
        "to": "voice_actor",
        "kind": "response",
        "payload": {},
        "timestamp": stale.isoformat(),
    }
    env["signature"] = sign(env, secret)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{core_url}/v0/respond", json=env) as r:
            assert r.status == 401
            data = await r.json()
            assert data["error"] == "stale_or_missing_timestamp"


async def test_invoke_rejects_replayed_envelope(core_server):
    core_url = core_server["url"]
    voice_secret = os.environ["VOICE_SECRET"]
    tasks_secret = os.environ["TASKS_SECRET"]

    tasks = MeshNode(node_id="tasks", secret=tasks_secret, core_url=core_url)
    tasks.on("list", lambda env: {"tasks": []})
    await tasks.start()
    voice = MeshNode(node_id="voice_actor", secret=voice_secret, core_url=core_url)
    await voice.connect()  # register but do not start dispatch loop
    try:
        msg_id = str(uuid.uuid4())
        env = {
            "id": msg_id,
            "correlation_id": msg_id,
            "from": "voice_actor",
            "to": "tasks.list",
            "kind": "invocation",
            "payload": {},
            "timestamp": now_iso(),
        }
        env["signature"] = sign(env, voice_secret)
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{core_url}/v0/invoke", json=env) as r:
                assert r.status == 200
            # Same envelope, replayed.
            async with s.post(f"{core_url}/v0/invoke", json=env) as r:
                assert r.status == 409
                data = await r.json()
                assert data["error"] == "replay_detected"
    finally:
        await asyncio.gather(tasks.stop(), voice.stop())
