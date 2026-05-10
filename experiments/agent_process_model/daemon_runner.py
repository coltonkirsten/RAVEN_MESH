"""
daemon_runner.py — long-lived process counterpart for benchmark fairness.

The cold-spawn runner forks a fresh interpreter per envelope. The daemon
runner forks ONE interpreter that loops, reading envelopes line-by-line
from stdin and writing one-line JSON responses to stdout. This lets the
benchmark compare like-for-like: same handler module, same payload shape,
only the process model differs.

This is also roughly the contract that today's `nodes/` would use if we
boiled them down to "stateless handler in a daemon" — which is what most
of them already are, modulo the SSE/web-server scaffolding.

Wire format on stdin: one JSON envelope per line (newline-delimited).
Wire format on stdout: one JSON response per line.
EOF on stdin -> graceful exit.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
import uuid
from typing import Awaitable, Callable

Handler = Callable[[dict], Awaitable[dict]]


def _build_response(env: dict, payload: dict, *, kind: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": env.get("id"),
        "from": env.get("to", "unknown"),
        "to": env.get("from", ""),
        "kind": kind,
        "payload": payload,
    }


async def _serve(handler: Handler) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        t0 = time.perf_counter()
        try:
            env = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({
                "kind": "error",
                "payload": {"reason": "invalid_json", "details": str(e)},
            }) + "\n")
            sys.stdout.flush()
            continue
        try:
            result = await handler(env)
            resp = _build_response(env, result or {}, kind="response")
        except Exception as e:
            resp = _build_response(env, {"reason": "handler_exception", "details": str(e)}, kind="error")
        resp["_handler_us"] = int((time.perf_counter() - t0) * 1_000_000)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m daemon_runner <module>:<async_handler_name>", file=sys.stderr)
        return 2
    target = argv[1]
    if ":" not in target:
        print("target must be module:handler", file=sys.stderr)
        return 2
    mod_name, attr = target.split(":", 1)
    mod = importlib.import_module(mod_name)
    handler = getattr(mod, attr)
    asyncio.run(_serve(handler))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
