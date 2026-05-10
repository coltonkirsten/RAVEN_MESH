"""echo node — minimal capability over NATS."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nats_node_sdk import NatsNode


async def ping(env):
    return {"echo": env["payload"].get("text", ""), "responder": "echo"}


async def main():
    manifest = os.path.join(os.path.dirname(__file__), "..", "manifest.yaml")
    node = NatsNode("echo", manifest)
    node.on("ping", ping)
    await node.start()
    print("[echo] up", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await node.stop()


if __name__ == "__main__":
    asyncio.run(main())
