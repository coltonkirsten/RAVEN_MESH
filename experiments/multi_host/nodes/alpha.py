"""alpha — actor on Core A that calls beta@B.

Connects, then sits idle. The demo runner triggers invocations via Core A's
admin /v0/admin/invoke synthesizer, OR alpha can be driven through the SDK
directly (see drive_alpha()).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from node_sdk import MeshNode


async def main_async(node_id: str, secret: str, core_url: str) -> int:
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.start()
    print(f"[alpha] connected. surfaces={[s['name'] for s in node.surfaces]} "
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
    p.add_argument("--node-id", default="alpha")
    p.add_argument("--core-url", default=os.environ.get("ALPHA_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--secret-env", default="ALPHA_SECRET")
    args = p.parse_args(argv)
    secret = os.environ.get(args.secret_env)
    if not secret:
        print(f"missing env var {args.secret_env}", file=sys.stderr)
        return 2
    return asyncio.run(main_async(args.node_id, secret, args.core_url))


if __name__ == "__main__":
    sys.exit(main())
