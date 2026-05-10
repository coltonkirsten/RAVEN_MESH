"""counter_node — tiny stateful target used by the replay demo.

Holds a single integer in process memory. Three surfaces:
    - increment({by})  : counter += by (default 1), returns new value
    - get({})          : returns current value
    - reset({})        : counter = 0

The point of this node is to give replay_node something with observable
side effects: re-firing the same envelope chain after a reset must drive
the counter to the same final value as the original chain, proving that
the audit log + admin/invoke really do reconstruct execution.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from node_sdk import MeshNode


class Counter:
    def __init__(self) -> None:
        self.value: int = 0

    def increment(self, by: int) -> int:
        self.value += by
        return self.value

    def reset(self) -> int:
        self.value = 0
        return 0


def make_handlers(state: Counter):
    async def on_increment(env: dict) -> dict:
        by = int(env.get("payload", {}).get("by", 1))
        new = state.increment(by)
        return {"value": new, "by": by}

    async def on_get(env: dict) -> dict:
        return {"value": state.value}

    async def on_reset(env: dict) -> dict:
        state.reset()
        return {"value": 0}

    return {"increment": on_increment, "get": on_get, "reset": on_reset}


async def run(node_id: str, secret: str, core_url: str) -> int:
    state = Counter()
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.connect()
    for name, h in make_handlers(state).items():
        node.on(name, h)
    await node.serve()

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    print(f"[{node_id}] counter_node ready", flush=True)
    await stop.wait()
    await node.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default="counter_node")
    p.add_argument("--secret-env", default="COUNTER_NODE_SECRET")
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    args = p.parse_args()
    secret = os.environ.get(args.secret_env)
    if not secret:
        print(f"missing env var {args.secret_env}", file=sys.stderr)
        return 2
    return asyncio.run(run(args.node_id, secret, args.core_url))


if __name__ == "__main__":
    sys.exit(main())
