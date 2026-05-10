"""beta — capability on Core B. Echoes input + records who called it."""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from node_sdk import MeshNode


async def ping_handler(env: dict) -> dict:
    return {
        "ok": True,
        "echo": env.get("payload", {}),
        "received_from": env.get("from"),
        "to": env.get("to"),
        "msg_id": env.get("id"),
        "served_by": "beta@B",
    }


async def slow_handler(env: dict) -> dict:
    delay = float(env.get("payload", {}).get("delay_seconds", 0.5))
    await asyncio.sleep(delay)
    return {
        "ok": True,
        "slept_for": delay,
        "received_from": env.get("from"),
        "served_by": "beta@B",
    }


async def main_async(node_id: str, secret: str, core_url: str) -> int:
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.connect()
    node.on("ping", ping_handler)
    node.on("slow", slow_handler)
    await node.serve()
    print(f"[beta] connected. surfaces={[s['name'] for s in node.surfaces]} "
          f"edges={node.relationships}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    await node.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default="beta")
    p.add_argument("--core-url", default=os.environ.get("BETA_CORE_URL", "http://127.0.0.1:8001"))
    p.add_argument("--secret-env", default="BETA_SECRET")
    args = p.parse_args(argv)
    secret = os.environ.get(args.secret_env)
    if not secret:
        print(f"missing env var {args.secret_env}", file=sys.stderr)
        return 2
    return asyncio.run(main_async(args.node_id, secret, args.core_url))


if __name__ == "__main__":
    sys.exit(main())
