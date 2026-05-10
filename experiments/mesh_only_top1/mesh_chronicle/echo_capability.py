"""Demo capability: echoes the payload back, with a per-call counter.

Stateful enough that responses differ on replay (counter increments). The
chronicle's diff surface uses this to show "look, the response changed
when we replayed it" — a real divergence that humans can reason about.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import signal
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from node_sdk import MeshNode  # noqa: E402


class EchoState:
    def __init__(self):
        self.count = 0

    async def __call__(self, env: dict) -> dict:
        self.count += 1
        return {
            "echoed": env.get("payload", {}),
            "call_index": self.count,
        }


async def run(node_id: str, secret: str, core_url: str) -> int:
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.connect()
    state = EchoState()
    for s in node.surfaces:
        if s["name"] == "ping":
            node.on("ping", state)
    await node.serve()
    print(f"[{node_id}] echo capability ready", flush=True)
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default="echo_capability")
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    args = p.parse_args()
    secret = os.environ.get("ECHO_CAPABILITY_SECRET")
    if not secret:
        print("missing env var ECHO_CAPABILITY_SECRET", file=sys.stderr)
        return 2
    return asyncio.run(run(args.node_id, secret, args.core_url))


if __name__ == "__main__":
    sys.exit(main())
