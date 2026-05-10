"""
spawn_sdk.py — Minimal envelope harness for cold-spawned RAVEN_MESH surfaces.

The mesh's existing node_sdk (node_sdk/__init__.py) is built around a
*long-lived* process: connect() registers with Core, serve() opens an SSE
stream, and a per-connection dispatcher loop fans deliver events out to
async handlers. That assumes the node stays running.

A cold-spawn surface flips the contract: the supervisor receives an envelope,
forks a fresh `python3 -m surface.module`, pipes the envelope to its stdin,
and reads the response off stdout. The child has no SSE stream, no
registration handshake, no relationships table. It also has no shared memory
across invocations — every invoke pays the interpreter startup cost.

This module provides the smallest possible harness so a handler written for
the cold-spawn world looks similar to the existing async-handler signature:

    from spawn_sdk import run_handler

    async def echo(env: dict) -> dict:
        return {"echo": env["payload"]}

    if __name__ == "__main__":
        run_handler(echo)

The HMAC signature on incoming envelopes is *checked* if SPAWN_SECRET is set,
matching the wire-protocol guarantee that node_sdk gets via Core. Output
envelopes are signed if SPAWN_SECRET is set so the runner can verify them.

Wire format on stdin: a single JSON envelope, terminated by EOF.
Wire format on stdout: a single JSON response envelope.

Errors are emitted as kind="error" with a `reason` field, mirroring
node_sdk.MeshDeny semantics.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import os
import sys
import traceback
import uuid
from typing import Any, Awaitable, Callable

Handler = Callable[[dict], Awaitable[Any]]


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _canonical(obj: dict) -> str:
    body = {k: v for k, v in obj.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _sign(obj: dict, secret: str) -> str:
    return hmac.new(secret.encode(), _canonical(obj).encode(), hashlib.sha256).hexdigest()


def _verify(env: dict, secret: str) -> bool:
    sig = env.get("signature")
    if not sig:
        return False
    return hmac.compare_digest(sig, _sign(env, secret))


def _build_response(original: dict, payload: dict, *, kind: str, secret: str | None) -> dict:
    env = {
        "id": str(uuid.uuid4()),
        "correlation_id": original.get("id"),
        "from": original.get("to", "unknown"),
        "to": original.get("from", ""),
        "kind": kind,
        "payload": payload,
        "timestamp": _now_iso(),
    }
    if secret:
        env["signature"] = _sign(env, secret)
    return env


def run_handler(handler: Handler) -> None:
    """Read one envelope from stdin, dispatch, write response to stdout, exit.

    Used by every cold-spawn surface as its entrypoint. The supervisor's
    spawn_runner is the only thing that should pipe envelopes into this.
    """
    raw = sys.stdin.read()
    secret = os.environ.get("SPAWN_SECRET")
    try:
        env = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stdout.write(json.dumps({
            "kind": "error",
            "payload": {"reason": "invalid_json", "details": str(e)},
        }))
        sys.stdout.flush()
        return

    if secret and not _verify(env, secret):
        resp = _build_response(env, {"reason": "bad_signature"}, kind="error", secret=secret)
        sys.stdout.write(json.dumps(resp))
        sys.stdout.flush()
        return

    try:
        result = asyncio.run(handler(env))
    except Exception as e:
        resp = _build_response(
            env,
            {"reason": "handler_exception", "details": str(e),
             "traceback": traceback.format_exc()[-500:]},
            kind="error",
            secret=secret,
        )
        sys.stdout.write(json.dumps(resp))
        sys.stdout.flush()
        return

    if result is None:
        # fire-and-forget surface — emit a small ack so the supervisor knows
        # the child exited cleanly.
        resp = _build_response(env, {"accepted": True}, kind="ack", secret=secret)
    else:
        resp = _build_response(env, result, kind="response", secret=secret)

    sys.stdout.write(json.dumps(resp))
    sys.stdout.flush()
