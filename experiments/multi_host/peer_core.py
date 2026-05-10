"""Federated Core entry point.

    python -m experiments.multi_host.peer_core --manifest manifestA.yaml --port 8000
    python -m experiments.multi_host.peer_core --manifest manifestB.yaml --port 8001

Same CLI surface as `core.core` but the manifest can carry `peer_cores` and
`remote_nodes` sections, and /v0/invoke is wired through `peer_link`.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from aiohttp import web

from experiments.multi_host.peer_link import make_federated_app


async def amain(manifest: str, host: str, port: int, audit: str) -> None:
    app = make_federated_app(manifest, audit)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    state = app["state"]
    fed = state.federation
    print(
        f"[peer-core {fed.local_name}] listening on http://{host}:{port}  "
        f"manifest={manifest} peers={list(fed.peers)} "
        f"remote_nodes={list(fed.remote_nodes)}",
        flush=True,
    )
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    print(f"[peer-core {fed.local_name}] shutting down", flush=True)
    await runner.cleanup()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RAVEN Mesh Federated Core")
    p.add_argument("--manifest", required=True)
    p.add_argument("--host", default=os.environ.get("MESH_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--audit-log", default=os.environ.get("AUDIT_LOG", "audit.log"))
    args = p.parse_args(argv)
    try:
        asyncio.run(amain(args.manifest, args.host, args.port, args.audit_log))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
