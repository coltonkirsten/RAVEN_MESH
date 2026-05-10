"""
weather.py — Cold-spawn surface that does meaningful but bounded work.

Stateless lookup against a tiny in-module table (no network — keeps the
benchmark deterministic). Represents the "I am a tool, I get called, I
return a value" use case that's the strongest cold-spawn candidate.
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from spawn_sdk import run_handler  # noqa: E402

_TABLE = {
    "sf": {"temp_f": 58, "cond": "fog"},
    "nyc": {"temp_f": 41, "cond": "clear"},
    "tokyo": {"temp_f": 67, "cond": "rain"},
    "sydney": {"temp_f": 79, "cond": "sun"},
}


async def weather(env: dict) -> dict:
    payload = env.get("payload", {}) or {}
    city = (payload.get("city") or "").lower()
    row = _TABLE.get(city)
    if row is None:
        return {"unknown_city": payload.get("city")}
    return {"city": city, **row}


if __name__ == "__main__":
    run_handler(weather)
