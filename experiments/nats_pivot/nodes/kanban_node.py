"""kanban node — capability with two surfaces, in-memory state."""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nats_node_sdk import NatsNode


CARDS: list[dict] = []


async def create(env):
    p = env["payload"]
    card = {
        "id": str(uuid.uuid4())[:8],
        "title": p["title"],
        "column": p.get("column", "backlog"),
    }
    CARDS.append(card)
    return {"card": card, "count": len(CARDS)}


async def list_cards(_env):
    return {"cards": CARDS, "count": len(CARDS)}


async def main():
    manifest = os.path.join(os.path.dirname(__file__), "..", "manifest.yaml")
    node = NatsNode("kanban", manifest)
    node.on("create", create)
    node.on("list", list_cards)
    await node.start()
    print("[kanban] up", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await node.stop()


if __name__ == "__main__":
    asyncio.run(main())
