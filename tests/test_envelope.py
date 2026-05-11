"""Envelope-level tests: HMAC sign/verify, schema validation edge cases."""
from __future__ import annotations

import json
import os
import pathlib

import aiohttp
import pytest
from jsonschema import ValidationError, validate as jsonschema_validate

from core.core import canonical, sign, verify
from node_sdk import MeshNode

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_sign_and_verify_round_trip():
    secret = "test-secret"
    env = {"id": "1", "from": "a", "to": "b.c", "kind": "invocation",
           "payload": {"x": 1}, "timestamp": "now"}
    env["signature"] = sign(env, secret)
    assert verify(env, secret) is True


def test_signature_excludes_signature_field():
    secret = "test-secret"
    env = {"id": "1", "payload": {}}
    s1 = sign(env, secret)
    env["signature"] = s1
    s2 = sign(env, secret)
    assert s1 == s2


def test_tampered_payload_fails_verify():
    secret = "test-secret"
    env = {"id": "1", "payload": {"x": 1}}
    env["signature"] = sign(env, secret)
    env["payload"]["x"] = 2
    assert verify(env, secret) is False


def test_wrong_secret_fails_verify():
    env = {"id": "1", "payload": {"x": 1}}
    env["signature"] = sign(env, "alpha")
    assert verify(env, "beta") is False


def test_canonical_is_stable():
    a = {"b": 2, "a": 1, "signature": "foo"}
    b = {"a": 1, "b": 2, "signature": "bar"}
    assert canonical(a) == canonical(b)


def test_schema_validate_passes():
    schema = json.loads((ROOT / "schemas" / "task_create.json").read_text())
    jsonschema_validate({"title": "buy milk"}, schema)


def test_schema_validate_rejects_missing_required():
    schema = json.loads((ROOT / "schemas" / "task_create.json").read_text())
    with pytest.raises(ValidationError):
        jsonschema_validate({}, schema)


def test_schema_validate_rejects_bad_color():
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["hex_color"],
        "properties": {
            "hex_color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}
        },
    }
    with pytest.raises(ValidationError):
        jsonschema_validate({"hex_color": "not-a-color"}, schema)
    jsonschema_validate({"hex_color": "#a1b2c3"}, schema)


async def test_register_rejects_bad_signature(core_server):
    url = core_server["url"]
    body = {"node_id": "tasks", "timestamp": "now"}
    body["signature"] = sign(body, "wrong-secret")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/v0/register", json=body) as r:
            assert r.status == 401


async def test_register_rejects_unknown_node(core_server):
    url = core_server["url"]
    body = {"node_id": "ghost", "timestamp": "now"}
    body["signature"] = sign(body, "anything")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/v0/register", json=body) as r:
            assert r.status == 404


async def test_invoke_rejects_bad_signature(core_server):
    url = core_server["url"]
    secret = os.environ["VOICE_SECRET"]
    node = MeshNode(node_id="voice_actor", secret=secret, core_url=url)
    await node.connect()  # legitimate registration first
    try:
        # Build an envelope and sign it with the WRONG secret.
        env = {
            "id": "x", "correlation_id": "x",
            "from": "voice_actor", "to": "tasks.list",
            "kind": "invocation", "payload": {}, "timestamp": "now",
        }
        env["signature"] = sign(env, "not-the-right-secret")
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/v0/invoke", json=env) as r:
                assert r.status == 401
    finally:
        await node.stop()
