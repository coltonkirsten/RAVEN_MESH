"""Dummy approval node: auto-approves (or auto-denies if DENY=1) every request.

Inbox payload schema (per schemas/approval_request.json):
    {target_surface: "<node>.<surface>", payload: {...}, reason?: "..."}

Behavior:
    - Approve: invoke the wrapped target with the wrapped payload, forward
      the response back to the original requester.
    - Deny:    return kind=error with reason=denied_by_human (or reason from env).

Usage:
    HUMAN_APPROVAL_SECRET=... python3 -m nodes.dummy.dummy_approval \\
        --node-id human_approval --core-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from node_sdk import MeshDeny, MeshError, MeshNode


async def run(node_id: str, secret: str, core_url: str, deny: bool) -> int:
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.connect()

    async def on_inbox(env: dict) -> dict:
        body = env.get("payload", {})
        target = body.get("target_surface")
        inner = body.get("payload", {})
        if deny:
            raise MeshDeny("denied_by_human", target=target)
        try:
            result = await node.invoke(target, inner, wrapped=env)
        except MeshError as e:
            raise MeshDeny("downstream_error", status=e.status, data=e.data) from e
        return result.get("payload", result) if isinstance(result, dict) else {"result": result}

    node.on("inbox", on_inbox)
    await node.serve()

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    print(f"[{node_id}] dummy_approval ready (deny={deny})", flush=True)
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
    deny = os.environ.get("DENY", "0") == "1"
    return asyncio.run(run(args.node_id, secret, args.core_url, deny))


if __name__ == "__main__":
    sys.exit(main())
