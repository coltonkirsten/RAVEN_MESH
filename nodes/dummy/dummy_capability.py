"""Dummy capability node: registers and echoes whatever it receives.

Stays connected until killed. Useful for protocol smoke tests.

Usage:
    DUMMY_CAPABILITY_SECRET=... python3 -m nodes.dummy.dummy_capability \\
        --node-id tasks --core-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from node_sdk import MeshNode


async def echo(env: dict) -> dict:
    return {"echo": env.get("payload", {}), "from": env.get("from"), "to": env.get("to")}


async def run(node_id: str, secret: str, core_url: str) -> int:
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.connect()
    for s in node.surfaces:
        node.on(s["name"], echo)
    await node.serve()

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    print(f"[{node_id}] dummy_capability ready. surfaces={[s['name'] for s in node.surfaces]}", flush=True)
    await stop.wait()
    await node.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", required=True)
    p.add_argument("--secret-env", default=None)
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    args = p.parse_args()
    secret_env = args.secret_env or f"{args.node_id.upper()}_SECRET"
    secret = os.environ.get(secret_env)
    if not secret:
        print(f"missing env var {secret_env}", file=sys.stderr)
        return 2
    return asyncio.run(run(args.node_id, secret, args.core_url))


if __name__ == "__main__":
    sys.exit(main())
