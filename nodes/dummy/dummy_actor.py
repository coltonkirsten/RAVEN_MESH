"""Dummy actor: sends one invocation on startup, prints response, exits.

Usage:
    DUMMY_ACTOR_SECRET=... python3 -m nodes.dummy.dummy_actor \\
        --node-id voice_actor \\
        --target tasks.list \\
        --payload '{}' \\
        --core-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from node_sdk import MeshNode, MeshError


async def run(node_id: str, secret: str, core_url: str, target: str, payload: dict, wait: bool) -> int:
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.start()
    try:
        result = await node.invoke(target, payload, wait=wait)
        print(json.dumps(result, indent=2))
        return 0
    except MeshError as e:
        print(f"error: {e.status} {e.data}", file=sys.stderr)
        return 1
    finally:
        await node.stop()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", required=True)
    p.add_argument("--secret-env", default=None,
                   help="env var holding this node's secret (default <NODE_ID>_SECRET upper)")
    p.add_argument("--target", required=True, help="target surface, e.g. tasks.list")
    p.add_argument("--payload", default="{}", help="JSON payload")
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--no-wait", action="store_true", help="fire_and_forget (don't wait for response)")
    args = p.parse_args()
    secret_env = args.secret_env or f"{args.node_id.upper()}_SECRET"
    secret = os.environ.get(secret_env)
    if not secret:
        print(f"missing env var {secret_env}", file=sys.stderr)
        return 2
    payload = json.loads(args.payload)
    return asyncio.run(run(args.node_id, secret, args.core_url, args.target, payload, not args.no_wait))


if __name__ == "__main__":
    sys.exit(main())
