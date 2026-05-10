"""
echo.py — Reference cold-spawn surface.

A cold-spawn surface is a python module whose __main__ block calls
spawn_sdk.run_handler() with one async handler. That's the entire contract.

Echo is the trivially-stateless example: takes a payload, returns it back.
We use it as the latency floor in benchmarks — any cost above this is pure
process-spawn overhead, not handler work.
"""
from __future__ import annotations

import sys
import pathlib

# Make the parent directory importable when running as a script
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from spawn_sdk import run_handler  # noqa: E402


async def echo(env: dict) -> dict:
    return {"echo": env.get("payload", {}), "from": env.get("from")}


if __name__ == "__main__":
    run_handler(echo)
