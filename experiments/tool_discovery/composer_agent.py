"""composer_agent — a mesh node that LLM-plans and executes tool chains.

Pattern:
  1. At boot, call ``discover_capabilities(self.node_id, ...)`` to enumerate
     every surface the manifest allows this node to invoke. Each surface
     comes back with its full JSON Schema attached.
  2. Translate the (address, schema) pairs into OpenAI's
     ``{"type":"function","function":{name,description,parameters}}`` tool
     format. Tool name is the mesh address with the dot replaced by ``__``
     (OpenAI tool names can't contain ``.``).
  3. Expose one surface — ``compose`` — that accepts ``{"goal": "..."}``,
     hands the goal + tool list to gpt-4o-mini, executes whatever tools the
     model picks via ``MeshNode.invoke``, feeds results back as
     ``role=tool`` messages, and loops until the model stops calling tools
     or ``max_steps`` is hit. Returns the full chain transcript.

Why this is interesting: the composer was *not* told what kanban or voice
or webui can do. It learned the mesh's capabilities at runtime by querying
Core's admin endpoint. Add a new node to the manifest, give the composer
an edge to it, restart — the new tool appears in the planner's toolkit
with zero code changes.

Falls back to a deterministic synthetic planner if OPENAI_API_KEY is
unset, so the end-to-end demo works without a key.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from typing import Any

import aiohttp

# Make the parent project importable when run via `python3 -m
# experiments.tool_discovery.composer_agent` OR as a plain script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from node_sdk import MeshNode, MeshError  # noqa: E402

from experiments.tool_discovery.mesh_introspect import discover_capabilities  # noqa: E402

log = logging.getLogger("composer")

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
ADDR_SEP = "__"   # mesh "node.surface" -> OpenAI "node__surface"


def addr_to_tool_name(address: str) -> str:
    """``kanban_node.create_card`` -> ``kanban_node__create_card``.

    OpenAI restricts tool names to ``[a-zA-Z0-9_-]{1,64}``. Mesh addresses
    use ``.`` which is illegal, so we substitute. Anything else weird gets
    sanitized too — defensive, since any node could have an odd surface
    name.
    """
    name = address.replace(".", ADDR_SEP)
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return name[:64]


def tool_name_to_addr(name: str) -> str:
    return name.replace(ADDR_SEP, ".", 1)


def capabilities_to_openai_tools(caps: list[dict]) -> list[dict]:
    """Translate discover_capabilities() output to OpenAI tool-call format."""
    tools = []
    for c in caps:
        schema = c.get("schema_dict") or {"type": "object"}
        # OpenAI wants a JSON Schema object — but rejects exotic keys like
        # ``$schema`` and ``additionalProperties: true`` in some configs.
        # Strip the meta keys and force ``additionalProperties: false`` only
        # if the original schema was strict, otherwise leave it alone.
        params = {k: v for k, v in schema.items() if not k.startswith("$")}
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        title = schema.get("title") or c["address"]
        desc = schema.get("description") or (
            f"Invoke {c['address']} on the mesh "
            f"({c['surface_type']}, {c['invocation_mode']})."
        )
        tools.append({
            "type": "function",
            "function": {
                "name": addr_to_tool_name(c["address"]),
                "description": f"[{c['address']}] {title}: {desc}"[:1024],
                "parameters": params,
            },
        })
    return tools


# ---------- LLM client ----------

async def call_openai(api_key: str, model: str, messages: list[dict],
                      tools: list[dict]) -> dict:
    """Single chat-completion round with tool-call support."""
    body = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(OPENAI_URL, headers=headers, json=body) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(f"openai {r.status}: {data}")
            return data["choices"][0]["message"]


def synthetic_plan(goal: str, caps: list[dict]) -> dict:
    """No-API-key fallback: pick a kanban/say tool from the goal text.

    Crude regex routing — exists only so the demo can run without an API
    key. The real planner is the LLM.
    """
    g = goal.lower()
    by_addr = {c["address"]: c for c in caps}
    if any(w in g for w in ("kanban", "card", "task", "todo")):
        addr = next((a for a in by_addr if a.endswith(".create_card")), None)
        if addr:
            title = re.sub(r"^\s*(create|add|make)\s+(a\s+)?(kanban\s+)?(task|card)\s+to\s+", "", goal, flags=re.I).strip()
            title = title or goal
            return {"tool_calls": [{
                "id": "synth_1",
                "type": "function",
                "function": {
                    "name": addr_to_tool_name(addr),
                    "arguments": json.dumps({"column": "Backlog", "title": title}),
                },
            }]}
    if any(w in g for w in ("say", "speak", "voice")):
        addr = next((a for a in by_addr if a.endswith(".say")), None)
        if addr:
            return {"tool_calls": [{
                "id": "synth_1",
                "type": "function",
                "function": {
                    "name": addr_to_tool_name(addr),
                    "arguments": json.dumps({"text": goal}),
                },
            }]}
    return {"content": f"(synthetic planner: no obvious tool for goal: {goal!r})"}


# ---------- composer node ----------

class ComposerAgent:
    def __init__(self, node_id: str, secret: str, core_url: str,
                 admin_token: str, model: str = DEFAULT_MODEL):
        self.node_id = node_id
        self.core_url = core_url
        self.admin_token = admin_token
        self.model = model
        self.api_key = os.environ.get("OPENAI_API_KEY")
        self.node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
        self.node.on("compose", self.handle_compose)
        self.capabilities: list[dict] = []
        self.tools: list[dict] = []

    async def boot(self) -> None:
        await self.node.start()
        # /v0/admin/state requires a manifest-loaded edge to even know we
        # exist; start() registers, but discover happens via Core admin.
        try:
            self.capabilities = discover_capabilities(
                self.node_id, self.core_url, self.admin_token,
            )
        except Exception as e:
            log.warning("discover_capabilities failed (%s) — falling back to "
                        "registration-supplied surfaces", e)
            # Fall back to whatever Core handed back during register.
            # Build a minimal cap list from self.node.relationships.
            self.capabilities = [{
                "target_node": rel["to"].split(".", 1)[0],
                "surface": rel["to"].split(".", 1)[1] if "." in rel["to"] else "",
                "address": rel["to"],
                "surface_type": "tool",
                "invocation_mode": "request_response",
                "schema_url": None,
                "schema_dict": {"type": "object", "additionalProperties": True},
            } for rel in self.node.relationships]
        self.tools = capabilities_to_openai_tools(self.capabilities)
        log.info("composer discovered %d capabilities -> %d openai tools",
                 len(self.capabilities), len(self.tools))
        for c in self.capabilities:
            log.info("  - %s (%s)", c["address"], c["invocation_mode"])

    async def handle_compose(self, env: dict) -> dict:
        payload = env.get("payload", {}) or {}
        goal = payload.get("goal", "")
        if not goal:
            return {"error": "missing_goal"}
        max_steps = int(payload.get("max_steps", 6))
        dry_run = bool(payload.get("dry_run", False))
        model = payload.get("model") or self.model
        chain, final = await self.run_chain(goal, model, max_steps, dry_run)
        return {
            "goal": goal,
            "model": model,
            "dry_run": dry_run,
            "tool_count": len(self.tools),
            "chain": chain,
            "final_message": final,
        }

    async def run_chain(self, goal: str, model: str, max_steps: int,
                        dry_run: bool) -> tuple[list[dict], str | None]:
        """The core planner loop: LLM picks tool -> we invoke -> feed back."""
        system = (
            "You are a mesh tool composer. Use the provided tools to "
            "accomplish the user's goal. Each tool maps 1:1 to a surface "
            "on the RAVEN Mesh; calling the tool actually runs it. "
            "Prefer short chains. When the goal is satisfied, reply with a "
            "natural-language summary and stop calling tools."
        )
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": goal},
        ]
        chain: list[dict] = []
        for step in range(max_steps):
            if self.api_key:
                try:
                    msg = await call_openai(self.api_key, model, messages, self.tools)
                except Exception as e:
                    log.exception("openai failed")
                    return chain, f"openai_error: {e}"
            else:
                msg = synthetic_plan(goal, self.capabilities)
                # synthetic planner only fires once.
                if step > 0:
                    msg = {"content": "(synthetic planner: done)"}

            tool_calls = msg.get("tool_calls") or []
            messages.append({
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": tool_calls or None,
            })
            if not tool_calls:
                return chain, msg.get("content")

            for tc in tool_calls:
                fn = tc["function"]
                tool_name = fn["name"]
                addr = tool_name_to_addr(tool_name)
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                step_record: dict[str, Any] = {
                    "step": step,
                    "tool_call_id": tc.get("id"),
                    "address": addr,
                    "arguments": args,
                }
                if dry_run:
                    step_record["result"] = {"_dry_run": True}
                    chain.append(step_record)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "name": tool_name,
                        "content": json.dumps({"_dry_run": True}),
                    })
                    continue
                try:
                    result = await self.node.invoke(addr, args)
                    step_record["result"] = result
                except MeshError as e:
                    step_record["error"] = {"status": e.status, "data": e.data}
                    result = {"error": str(e)}
                except Exception as e:
                    step_record["error"] = {"status": "exception", "data": str(e)}
                    result = {"error": str(e)}
                chain.append(step_record)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "name": tool_name,
                    "content": json.dumps(step_record.get("result") or step_record.get("error")),
                })
        return chain, "(max_steps reached)"

    async def stop(self) -> None:
        await self.node.stop()


# ---------- entrypoint ----------

async def run(node_id: str, secret: str, core_url: str,
              admin_token: str, model: str) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    agent = ComposerAgent(node_id, secret, core_url, admin_token, model)
    await agent.boot()
    print(f"[{node_id}] composer ready. capabilities={len(agent.capabilities)} "
          f"tools={len(agent.tools)} api_key={'yes' if agent.api_key else 'NO (synthetic mode)'}",
          flush=True)
    stop = asyncio.Event()
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await agent.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default="composer_agent")
    p.add_argument("--secret-env", default=None)
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", "admin-dev-token"))
    p.add_argument("--model", default=os.environ.get("COMPOSER_MODEL", DEFAULT_MODEL))
    args = p.parse_args()
    secret_env = args.secret_env or f"{args.node_id.upper()}_SECRET"
    secret = os.environ.get(secret_env)
    if not secret:
        print(f"missing env var {secret_env}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(args.node_id, secret, args.core_url,
                               args.admin_token, args.model))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
