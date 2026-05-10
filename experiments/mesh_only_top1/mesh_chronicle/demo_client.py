"""Drives demo traffic: client_actor invokes echo_capability.ping a few times.

After the burst, queries chronicle for the captured chains and prints them.
This is a one-shot script, not a long-running node — it registers, sends,
quits.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from node_sdk import MeshNode  # noqa: E402


async def run(core_url: str, count: int) -> int:
    secret = os.environ.get("CLIENT_ACTOR_SECRET")
    if not secret:
        print("missing CLIENT_ACTOR_SECRET", file=sys.stderr)
        return 2
    node = MeshNode(node_id="client_actor", secret=secret, core_url=core_url)
    await node.connect()
    await node.serve()

    print(f"[client_actor] sending {count} pings to echo_capability.ping ...", flush=True)
    # A mix: half match a hypothetical-future v2 schema (`user_id: u_*`),
    # half don't (legacy payloads from before user_id existed). Under v1
    # all of them pass; under v2 the legacy ones break.
    for i in range(count):
        if i % 2 == 0:
            payload = {"text": f"legacy ping #{i}", "session": "abc"}
        else:
            payload = {"text": f"new ping #{i}", "user_id": f"u_demo{i}"}
        result = await node.invoke("echo_capability.ping", payload)
        ci = result.get("payload", {}).get("call_index")
        print(f"  ping#{i} payload={payload} -> call_index={ci}", flush=True)
        await asyncio.sleep(0.05)

    print("[client_actor] querying chronicle.list_chains ...", flush=True)
    chains = await node.invoke("mesh_chronicle.list_chains", {"limit": 20})
    print(json.dumps(chains, indent=2), flush=True)

    await node.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--count", type=int, default=5)
    args = p.parse_args()
    return asyncio.run(run(args.core_url, args.count))


if __name__ == "__main__":
    sys.exit(main())
