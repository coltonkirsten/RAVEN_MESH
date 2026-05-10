"""Timestamp-window check on /v0/register.

Register envelopes lack a unique id field, so nonce-LRU dedup isn't possible
without an SDK protocol change. Until that bump, the timestamp-only check
narrows the replay window to MESH_REPLAY_WINDOW_S — an attacker who captures
a register envelope can still replay it within the window (documented gap),
but cannot replay one captured from yesterday.
"""
from __future__ import annotations

import datetime as _dt
import os

import aiohttp

from core.core import now_iso, sign


def _register_body(node_id: str, secret: str, *, timestamp: str | None) -> dict:
    body: dict = {"node_id": node_id}
    if timestamp is not None:
        body["timestamp"] = timestamp
    body["signature"] = sign(body, secret)
    return body


async def test_register_accepts_fresh_timestamp(core_server):
    secret = os.environ["VOICE_SECRET"]
    body = _register_body("voice_actor", secret, timestamp=now_iso())
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{core_server['url']}/v0/register", json=body) as r:
            assert r.status == 200, await r.text()
            data = await r.json()
            assert "session_id" in data
            assert data["node_id"] == "voice_actor"


async def test_register_accepts_within_window(core_server):
    secret = os.environ["VOICE_SECRET"]
    ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)).isoformat()
    body = _register_body("voice_actor", secret, timestamp=ts)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{core_server['url']}/v0/register", json=body) as r:
            assert r.status == 200, await r.text()


async def test_register_rejects_stale_timestamp(core_server):
    state = core_server["state"]
    state.replay_window_s = 5

    secret = os.environ["VOICE_SECRET"]
    ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=70)).isoformat()
    body = _register_body("voice_actor", secret, timestamp=ts)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{core_server['url']}/v0/register", json=body) as r:
            assert r.status == 401
            data = await r.json()
            assert data["error"] == "stale_register"
            assert "timestamp" in data["reason"]


async def test_register_rejects_missing_timestamp(core_server):
    secret = os.environ["VOICE_SECRET"]
    body: dict = {"node_id": "voice_actor"}
    body["signature"] = sign(body, secret)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{core_server['url']}/v0/register", json=body) as r:
            assert r.status == 401
            data = await r.json()
            assert data["error"] == "stale_register"


async def test_register_replay_within_window_still_accepted(core_server):
    """Documented gap: register envelopes have no unique id, so we cannot
    nonce-dedup. A captured envelope replayed within MESH_REPLAY_WINDOW_S
    still succeeds. Closing this requires adding a unique id to the register
    envelope schema (SDK change, deferred to next SDK bump).
    """
    secret = os.environ["VOICE_SECRET"]
    body = _register_body("voice_actor", secret, timestamp=now_iso())
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{core_server['url']}/v0/register", json=body) as r:
            assert r.status == 200
            first = await r.json()
        async with s.post(f"{core_server['url']}/v0/register", json=body) as r:
            assert r.status == 200
            second = await r.json()
    # Re-registering creates a fresh session_id (connection takeover).
    assert first["session_id"] != second["session_id"]
