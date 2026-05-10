"""
spawn_runner.py — proof-of-concept cold-spawn dispatcher for RAVEN_MESH.

The current mesh boots every node as a long-lived python3 process via
scripts/run_<node_id>.sh, supervised by core/supervisor.py. That's good for
nodes that hold state (voice_actor's Realtime websocket, kanban_node's
SSE hub, cron_node's task scheduler) but bad for nodes that don't:
every idle handler still pays ~10–30 MB of resident Python.

This runner demonstrates the alternative. Given an envelope addressed to a
known cold-spawn surface, it:

    1. Looks up the surface in a registry (surface_id -> module path).
    2. Forks `python3 -m <module>` with stdin = envelope JSON.
    3. Reads one JSON response off stdout.
    4. Returns the response (verifying signature if SPAWN_SECRET set).

It does NOT register with Core, run an SSE stream, or hold long-lived
state. The runner *itself* is intended to live inside Core as a new
"on_demand" arm of the supervisor (see ANALYSIS.md for the proposed diff).

Usage:

    runner = SpawnRunner.from_default_registry()
    resp = await runner.dispatch(envelope)

The runner accepts an optional `process_pool` size to bound concurrent
spawns. Beyond that, requests queue. This matches AWS Lambda's
"reserved concurrency" knob: every cold-spawn is independent, but the host
is finite, so we cap it.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import pathlib
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("spawn_runner")

# A registry entry binds a logical surface id (e.g. "echo.invoke") to a
# python module path that, when run with `python3 -m <module>`, reads one
# envelope from stdin and writes one response to stdout.
DEFAULT_REGISTRY: dict[str, str] = {
    "echo.invoke": "cold_handlers.echo",
    "weather.lookup": "cold_handlers.weather",
}


@dataclass
class SpawnResult:
    response: dict
    spawn_ms: float        # process startup time only
    handler_ms: float      # time inside the child after stdin closed
    total_ms: float        # wall-clock from dispatch entry to dispatch exit
    rc: int
    stderr: str
    stdout_bytes: int = 0


@dataclass
class SpawnRunner:
    registry: dict[str, str]
    python: str = sys.executable
    cwd: str = field(default_factory=lambda: str(pathlib.Path(__file__).resolve().parent))
    secret: Optional[str] = None
    max_concurrent: int = 16
    spawn_timeout_s: float = 15.0
    _sem: Optional[asyncio.Semaphore] = field(default=None, init=False, repr=False)

    @classmethod
    def from_default_registry(cls, **kwargs) -> "SpawnRunner":
        return cls(registry=dict(DEFAULT_REGISTRY), **kwargs)

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrent)
        return self._sem

    async def dispatch(self, envelope: dict) -> SpawnResult:
        """Spawn a fresh subprocess for envelope['to'] and return its response.

        Raises ValueError for unknown surface. Other failures are encoded in
        the response payload as kind=error so the caller can treat them
        uniformly with handler-raised errors.
        """
        target = envelope.get("to")
        if not target or target not in self.registry:
            raise ValueError(f"no cold-spawn handler for surface={target!r}")
        module = self.registry[target]
        sem = self._semaphore()
        async with sem:
            return await self._run_one(module, envelope)

    async def _run_one(self, module: str, envelope: dict) -> SpawnResult:
        env = dict(os.environ)
        if self.secret:
            env["SPAWN_SECRET"] = self.secret
            envelope = dict(envelope)
            envelope["signature"] = _sign(envelope, self.secret)

        payload = json.dumps(envelope).encode()

        t_dispatch = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            self.python, "-m", module,
            cwd=self.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        t_after_spawn = time.perf_counter()

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload),
                timeout=self.spawn_timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise

        t_done = time.perf_counter()
        rc = proc.returncode if proc.returncode is not None else -1

        try:
            response = json.loads(stdout.decode("utf-8")) if stdout else {}
        except json.JSONDecodeError as e:
            response = {
                "kind": "error",
                "payload": {"reason": "child_emitted_invalid_json", "details": str(e)},
            }

        if self.secret and response.get("signature"):
            if not _verify(response, self.secret):
                response = {
                    "kind": "error",
                    "payload": {"reason": "bad_response_signature"},
                }

        return SpawnResult(
            response=response,
            spawn_ms=(t_after_spawn - t_dispatch) * 1000,
            handler_ms=(t_done - t_after_spawn) * 1000,
            total_ms=(t_done - t_dispatch) * 1000,
            rc=rc,
            stderr=stderr.decode("utf-8", errors="replace") if stderr else "",
            stdout_bytes=len(stdout),
        )


# ---------- envelope signing helpers (mirrors node_sdk) ----------

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _canonical(obj: dict) -> str:
    body = {k: v for k, v in obj.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _sign(obj: dict, secret: str) -> str:
    return hmac.new(secret.encode(), _canonical(obj).encode(), hashlib.sha256).hexdigest()


def _verify(obj: dict, secret: str) -> bool:
    sig = obj.get("signature")
    if not sig:
        return False
    return hmac.compare_digest(sig, _sign(obj, secret))


def make_envelope(*, frm: str, to: str, payload: dict) -> dict:
    """Construct a wire-shaped envelope addressed to a cold-spawn surface."""
    msg_id = str(uuid.uuid4())
    return {
        "id": msg_id,
        "correlation_id": msg_id,
        "from": frm,
        "to": to,
        "kind": "invocation",
        "payload": payload,
        "timestamp": _now_iso(),
    }


# ---------- CLI for ad-hoc testing ----------

async def _cli_main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: python -m spawn_runner <surface> '<json_payload>'", file=sys.stderr)
        return 2
    surface = argv[1]
    payload = json.loads(argv[2]) if len(argv) > 2 else {}
    runner = SpawnRunner.from_default_registry()
    env = make_envelope(frm="cli", to=surface, payload=payload)
    res = await runner.dispatch(env)
    print(json.dumps({
        "spawn_ms": round(res.spawn_ms, 2),
        "handler_ms": round(res.handler_ms, 2),
        "total_ms": round(res.total_ms, 2),
        "rc": res.rc,
        "response": res.response,
        "stderr_tail": res.stderr[-400:] if res.stderr else "",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_cli_main(sys.argv)))
