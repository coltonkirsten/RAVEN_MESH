"""dashboard node — pure caller. Drives a few invocations and prints results."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nats_node_sdk import NatsNode, MeshError


async def main():
    manifest = os.path.join(os.path.dirname(__file__), "..", "manifest.yaml")
    node = NatsNode("dashboard", manifest)
    await node.start()
    print("[dashboard] up", flush=True)
    try:
        r = await node.invoke("echo.ping", {"text": "hello mesh"})
        print(f"[dashboard] echo.ping -> {r}", flush=True)

        r = await node.invoke("kanban.create", {"title": "first card"})
        print(f"[dashboard] kanban.create -> {r}", flush=True)

        r = await node.invoke("kanban.create", {"title": "second card", "column": "doing"})
        print(f"[dashboard] kanban.create -> {r}", flush=True)

        r = await node.invoke("kanban.list", {})
        print(f"[dashboard] kanban.list -> {r}", flush=True)

        # --- schema validation: error envelope returned by responder SDK ---
        bad = await node.invoke("kanban.create", {"oops": "missing title"})
        print(f"[dashboard] kanban.create(bad) -> {bad}", flush=True)
        assert bad.get("kind") == "error", "schema validation should fail"

        # --- broker-level ACL: publishing to a subject the manifest did not
        # grant. The broker drops the publish silently; client sees timeout.
        try:
            await node.invoke("echo.unknown_surface", {"text": "x"}, timeout=1.0)
        except MeshError as e:
            print(f"[dashboard] echo.unknown_surface -> {e} (denied by broker ACL)", flush=True)
    finally:
        await node.stop()


if __name__ == "__main__":
    asyncio.run(main())
