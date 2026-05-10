"""MCP stdio server bridging Claude Code (the harness) to the RAVEN Mesh.

Identical in spirit to nodes/nexus_agent/mcp_bridge.py but baked into the
container image so the agent's runtime is fully self-contained. Reads its
config from env (NEXUS_AGENT_CONTROL_URL, NEXUS_AGENT_CONTROL_TOKEN,
NEXUS_AGENT_LEDGER_DIR). When run inside the docker container the
control URL points at host.docker.internal:<control_port>.

Tools exposed:
    mesh_list_surfaces, mesh_invoke, mesh_send_to_inbox,
    memory_read, memory_write, list_skills, read_skill
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import aiohttp
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types


CONTROL_URL = os.environ.get(
    "NEXUS_AGENT_CONTROL_URL", "http://host.docker.internal:8816"
)
CONTROL_TOKEN = os.environ.get("NEXUS_AGENT_CONTROL_TOKEN", "")
LEDGER_DIR = os.environ.get("NEXUS_AGENT_LEDGER_DIR", "/agent/ledger")


server: Server = Server("nexus-agent-bridge")


async def _control(method: str, path: str, body: dict | None = None) -> dict:
    headers = {"X-Control-Token": CONTROL_TOKEN} if CONTROL_TOKEN else {}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        async with s.request(method, f"{CONTROL_URL}{path}", json=body) as r:
            text = await r.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {"raw": text}
            if r.status >= 400:
                return {"error": True, "status": r.status, "data": data}
            return data


def _text(payload: Any) -> list[types.TextContent]:
    if isinstance(payload, str):
        return [types.TextContent(type="text", text=payload)]
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


TOOLS: list[types.Tool] = [
    types.Tool(
        name="mesh_list_surfaces",
        description=(
            "List every mesh surface this agent can reach (i.e. surfaces it has a "
            "relationship edge to). Returns a list of {target, kind, mode, schema}."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="mesh_invoke",
        description=(
            "Invoke another node's tool surface and return its response. Use this "
            "for request/response interactions. Surface format: '<node_id>.<surface>'. "
            "Example: target_surface='webui_node.show_message', payload={'text':'hi'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_surface": {"type": "string", "description": "Fully qualified surface id (node.surface)"},
                "payload": {"type": "object", "description": "Surface input payload (must match the surface's schema)"},
            },
            "required": ["target_surface", "payload"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="mesh_send_to_inbox",
        description=(
            "Fire-and-forget message to another actor's inbox. No response. "
            "Use this to wake up another agent or hand off a task. The target "
            "should be a node id whose inbox surface is fire_and_forget."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_node": {"type": "string", "description": "Target node id (the .inbox is implied)"},
                "payload": {"type": "object", "description": "Message body (typically {'text': '...'})"},
            },
            "required": ["target_node", "payload"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="memory_read",
        description="Read this agent's persistent memory file (ledger/memory.md).",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="memory_write",
        description=(
            "Write to ledger/memory.md. mode='replace' overwrites the file; "
            "mode='append' appends with a leading newline. Use sparingly — "
            "memory is the agent's stable across-session state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["replace", "append"], "default": "replace"},
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="list_skills",
        description="List available skill files in ledger/skills/.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="read_skill",
        description="Read a skill file by name (with or without the .md extension).",
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
]


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "mesh_list_surfaces":
            data = await _control("GET", "/surfaces")
            return _text(data)

        if name == "mesh_invoke":
            data = await _control("POST", "/invoke", {
                "target_surface": arguments["target_surface"],
                "payload": arguments.get("payload", {}),
            })
            return _text(data)

        if name == "mesh_send_to_inbox":
            data = await _control("POST", "/send_inbox", {
                "target_node": arguments["target_node"],
                "payload": arguments.get("payload", {}),
            })
            return _text(data)

        if name == "memory_read":
            data = await _control("GET", "/memory")
            return _text(data.get("content", ""))

        if name == "memory_write":
            data = await _control("POST", "/memory", {
                "content": arguments["content"],
                "mode": arguments.get("mode", "replace"),
            })
            return _text(data)

        if name == "list_skills":
            data = await _control("GET", "/skills")
            return _text(data)

        if name == "read_skill":
            data = await _control("GET", f"/skills/{arguments['name']}")
            return _text(data.get("content", data))

        return _text({"error": f"unknown tool {name}"})
    except Exception as e:
        return _text({"error": str(e)})


async def _amain() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
