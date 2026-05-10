"""Nexus Agent — RAVEN Mesh actor that runs Claude Code as its harness.

On startup:
  1. Registers with Core via the MeshNode SDK (handlers: inbox, status).
  2. Boots an internal "control" HTTP server on loopback (default :8814).
     The MCP bridge subprocess calls back into this server to invoke other
     mesh surfaces and read/write the agent's ledger.
  3. Boots the inspector UI server on :8804 (web/server.py).

When inbox receives a message:
  - Logs it to data/logs/{ts}-{msg_id}.json
  - Spawns the `claude` CLI via cli_runner.run_claude(...)
  - Streams every cli event onto an internal pubsub bus that the inspector
    UI subscribes to over SSE.
  - When the run completes, the result text is sent back as a response if
    request_response, or just logged if fire_and_forget (default for inbox).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import pathlib
import secrets
import signal
import sys
import uuid
from typing import Any

from aiohttp import web

from node_sdk import MeshError, MeshNode

from .cli_runner import run_claude
from .web.server import make_inspector_app, AgentInspectorState

log = logging.getLogger("nexus_agent")

NODE_DIR = pathlib.Path(__file__).resolve().parent
LEDGER_DIR = NODE_DIR / "ledger"
SKILLS_DIR = LEDGER_DIR / "skills"
DATA_DIR = NODE_DIR / "data"
LOGS_DIR = DATA_DIR / "logs"
SESSIONS_DIR = DATA_DIR / "sessions"
SESSION_FILE = SESSIONS_DIR / "current.json"
BRIDGE_PATH = NODE_DIR / "mcp_bridge.py"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ---------- agent state ----------


class AgentRuntime:
    """Holds the live state of the agent across inbox messages."""

    def __init__(self, node: MeshNode, model: str, inspector: AgentInspectorState):
        self.node = node
        self.model = model
        self.inspector = inspector
        self.control_token = secrets.token_urlsafe(24)
        self.session_id: str | None = self._load_session()
        self.ui_visible = True
        self._lock = asyncio.Lock()  # serialize claude runs (one at a time)
        self.run_count = 0

    # session persistence ---------------------------------------------

    def _load_session(self) -> str | None:
        try:
            data = json.loads(SESSION_FILE.read_text())
            return data.get("session_id")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _save_session(self) -> None:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps({
            "session_id": self.session_id,
            "updated_at": _now(),
        }, indent=2))

    # logging ---------------------------------------------------------

    def log_message(self, env: dict, kind: str = "incoming") -> pathlib.Path:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        msg_id = env.get("id", "unknown")[:8]
        path = LOGS_DIR / f"{ts}-{kind}-{msg_id}.json"
        path.write_text(json.dumps(env, indent=2, default=str))
        return path

    # system prompt ---------------------------------------------------

    def system_prompt(self) -> str:
        identity = (LEDGER_DIR / "identity.md").read_text()
        try:
            memory = (LEDGER_DIR / "memory.md").read_text()
        except FileNotFoundError:
            memory = ""
        skills = sorted(p.name for p in SKILLS_DIR.glob("*.md")) if SKILLS_DIR.exists() else []
        skill_list = ", ".join(skills) if skills else "(none)"
        return (
            f"{identity}\n\n"
            f"---\n# Memory (mutable, shared across sessions)\n\n{memory}\n\n"
            f"---\n# Available skills (read with `read_skill`)\n\n{skill_list}\n"
        )

    # the actual agent loop -------------------------------------------

    async def handle_inbox(self, env: dict) -> dict | None:
        async with self._lock:
            self.run_count += 1
            self.log_message(env, "incoming")
            text = env.get("payload", {}).get("text") or json.dumps(env.get("payload", {}))
            await self.inspector.publish("user_message", {
                "id": env.get("id"),
                "from": env.get("from"),
                "text": text,
                "timestamp": env.get("timestamp"),
            })

            async def on_event(kind: str, data: dict[str, Any]) -> None:
                await self.inspector.publish(kind, data)

            try:
                result = await run_claude(
                    message=text,
                    system_prompt=self.system_prompt(),
                    bridge_path=BRIDGE_PATH,
                    control_url=f"http://127.0.0.1:{self.inspector.control_port}",
                    control_token=self.control_token,
                    ledger_dir=LEDGER_DIR,
                    model=self.model,
                    session_id=self.session_id,
                    on_event=on_event,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("claude run failed")
                await self.inspector.publish("run_error", {"error": str(e)})
                return None

            if result.session_id:
                self.session_id = result.session_id
                self._save_session()

            self.inspector.last_result = {
                "text": result.result_text,
                "session_id": result.session_id,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "is_error": result.is_error,
                "at": _now(),
            }
            await self.inspector.publish("run_done", self.inspector.last_result)

            self.log_message({
                "id": str(uuid.uuid4()),
                "in_reply_to": env.get("id"),
                "result": self.inspector.last_result,
            }, "result")

            # Inbox is fire-and-forget by manifest, but if a caller invoked us
            # request_response we'll still return a payload — node_sdk drops it
            # when the surface is FAF.
            return {
                "ok": not result.is_error,
                "text": result.result_text,
                "session_id": result.session_id,
                "tokens": {"in": result.input_tokens, "out": result.output_tokens},
            }

    async def handle_status(self, env: dict) -> dict:
        return {
            "node_id": self.node.node_id,
            "model": self.model,
            "session_id": self.session_id,
            "run_count": self.run_count,
            "ui_visible": self.ui_visible,
            "last_result": self.inspector.last_result,
            "now": _now(),
        }

    async def handle_ui_visibility(self, env: dict) -> dict:
        action = env.get("payload", {}).get("action", "show")
        if action == "hide":
            self.ui_visible = False
        elif action == "show":
            self.ui_visible = True
        await self.inspector.publish("ui_visibility", {"visible": self.ui_visible})
        # Best-effort report to Core's admin (added by worker A; safe to fail).
        asyncio.create_task(self._report_visibility())
        return {"ok": True, "visible": self.ui_visible}

    async def _report_visibility(self) -> None:
        import aiohttp as _aiohttp
        token = os.environ.get("ADMIN_TOKEN", "admin-dev-token")
        body = {"node_id": self.node.node_id, "visible": self.ui_visible}
        try:
            timeout = _aiohttp.ClientTimeout(total=3)
            async with _aiohttp.ClientSession(timeout=timeout) as s:
                await s.post(
                    f"{self.node.core_url}/v0/admin/node_status",
                    json=body,
                    headers={"X-Admin-Token": token},
                )
        except Exception:
            pass  # admin endpoint may not exist yet


# ---------- control server (loopback only — bridge calls these) ----------


def make_control_app(rt: AgentRuntime) -> web.Application:
    app = web.Application()

    @web.middleware
    async def auth(request: web.Request, handler):
        # Loopback-only is enforced by binding to 127.0.0.1, but we still check
        # a token to keep other local processes from poking the bridge.
        if request.headers.get("X-Control-Token") != rt.control_token:
            return web.json_response({"error": "forbidden"}, status=403)
        return await handler(request)

    app.middlewares.append(auth)

    async def surfaces(request: web.Request) -> web.Response:
        # The MeshNode SDK gives us our outgoing edges via /v0/introspect.
        # We re-derive what we can call from the introspect view.
        out = []
        try:
            assert rt.node._http is not None
            async with rt.node._http.get(f"{rt.node.core_url}/v0/introspect") as r:
                data = await r.json()
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": str(e)}, status=502)

        # Build a quick index of surfaces by node
        node_index = {n["id"]: n for n in data.get("nodes", [])}
        for edge in data.get("relationships", []):
            if edge["from"] != rt.node.node_id:
                continue
            target_node, _, surface_name = edge["to"].partition(".")
            ndecl = node_index.get(target_node, {})
            sdecl = next(
                (s for s in ndecl.get("surfaces", []) if s["name"] == surface_name),
                None,
            )
            out.append({
                "target": edge["to"],
                "kind": ndecl.get("kind"),
                "type": (sdecl or {}).get("type"),
                "mode": (sdecl or {}).get("invocation_mode"),
            })
        return web.json_response({"surfaces": out})

    async def invoke(request: web.Request) -> web.Response:
        body = await request.json()
        target = body["target_surface"]
        payload = body.get("payload", {})
        await rt.inspector.publish("tool_call", {
            "tool": "mesh_invoke", "target": target, "payload": payload,
        })
        try:
            result = await rt.node.invoke(target, payload, wait=True)
            await rt.inspector.publish("tool_result", {
                "tool": "mesh_invoke", "target": target, "result": result,
            })
            return web.json_response(result)
        except MeshError as e:
            err = {"error": True, "status": e.status, "data": e.data}
            await rt.inspector.publish("tool_result", {
                "tool": "mesh_invoke", "target": target, "result": err,
            })
            return web.json_response(err, status=200)
        except Exception as e:  # noqa: BLE001
            err = {"error": True, "message": str(e)}
            await rt.inspector.publish("tool_result", {
                "tool": "mesh_invoke", "target": target, "result": err,
            })
            return web.json_response(err, status=200)

    async def send_inbox(request: web.Request) -> web.Response:
        body = await request.json()
        target_node = body["target_node"]
        target = f"{target_node}.inbox"
        payload = body.get("payload", {})
        await rt.inspector.publish("tool_call", {
            "tool": "mesh_send_to_inbox", "target": target, "payload": payload,
        })
        try:
            result = await rt.node.invoke(target, payload, wait=False)
            await rt.inspector.publish("tool_result", {
                "tool": "mesh_send_to_inbox", "target": target, "result": result,
            })
            return web.json_response(result)
        except MeshError as e:
            err = {"error": True, "status": e.status, "data": e.data}
            return web.json_response(err, status=200)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": True, "message": str(e)}, status=200)

    async def memory_get(request: web.Request) -> web.Response:
        path = LEDGER_DIR / "memory.md"
        try:
            content = path.read_text()
        except FileNotFoundError:
            content = ""
        return web.json_response({"content": content})

    async def memory_post(request: web.Request) -> web.Response:
        body = await request.json()
        content = body.get("content", "")
        mode = body.get("mode", "replace")
        path = LEDGER_DIR / "memory.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with path.open("a") as f:
                f.write("\n" + content)
        else:
            path.write_text(content)
        await rt.inspector.publish("memory_write", {"mode": mode, "bytes": len(content)})
        return web.json_response({"ok": True, "mode": mode, "bytes": len(content)})

    async def skills_list(request: web.Request) -> web.Response:
        if not SKILLS_DIR.exists():
            return web.json_response({"skills": []})
        names = sorted(p.name for p in SKILLS_DIR.glob("*.md"))
        return web.json_response({"skills": names})

    async def skill_get(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if not name.endswith(".md"):
            name = f"{name}.md"
        path = SKILLS_DIR / name
        # Refuse path traversal
        if not path.resolve().is_relative_to(SKILLS_DIR.resolve()):
            return web.json_response({"error": "bad path"}, status=400)
        try:
            return web.json_response({"name": name, "content": path.read_text()})
        except FileNotFoundError:
            return web.json_response({"error": f"skill {name} not found"}, status=404)

    app.router.add_get("/surfaces", surfaces)
    app.router.add_post("/invoke", invoke)
    app.router.add_post("/send_inbox", send_inbox)
    app.router.add_get("/memory", memory_get)
    app.router.add_post("/memory", memory_post)
    app.router.add_get("/skills", skills_list)
    app.router.add_get("/skills/{name}", skill_get)
    return app


# ---------- bootstrap ----------


async def run(
    node_id: str,
    secret: str,
    core_url: str,
    model: str,
    inspector_host: str,
    inspector_port: int,
    control_port: int,
) -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    inspector = AgentInspectorState(
        node_id=node_id,
        ledger_dir=LEDGER_DIR,
        skills_dir=SKILLS_DIR,
        logs_dir=LOGS_DIR,
        control_port=control_port,
        model=model,
    )

    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    rt = AgentRuntime(node=node, model=model, inspector=inspector)
    inspector.runtime = rt

    node.on("inbox", rt.handle_inbox)
    node.on("status", rt.handle_status)
    node.on("ui_visibility", rt.handle_ui_visibility)

    await node.start()

    # control loopback
    control_app = make_control_app(rt)
    control_runner = web.AppRunner(control_app)
    await control_runner.setup()
    control_site = web.TCPSite(control_runner, "127.0.0.1", control_port)
    await control_site.start()
    log.info("[%s] control server: http://127.0.0.1:%s (token=%s…)",
             node_id, control_port, rt.control_token[:6])

    # inspector UI
    inspector_app = make_inspector_app(inspector, rt)
    inspector_runner = web.AppRunner(inspector_app)
    await inspector_runner.setup()
    inspector_site = web.TCPSite(inspector_runner, inspector_host, inspector_port)
    await inspector_site.start()
    log.info("[%s] inspector UI: http://%s:%s", node_id, inspector_host, inspector_port)

    print(f"[{node_id}] nexus_agent ready. inspector: http://{inspector_host}:{inspector_port}",
          flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    await control_runner.cleanup()
    await inspector_runner.cleanup()
    await node.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default="nexus_agent")
    p.add_argument("--secret-env", default=None)
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--model", default=os.environ.get("NEXUS_AGENT_MODEL", "claude-sonnet-4-6"))
    p.add_argument("--inspector-host", default=os.environ.get("NEXUS_AGENT_HOST", "127.0.0.1"))
    p.add_argument("--inspector-port", type=int,
                   default=int(os.environ.get("NEXUS_AGENT_PORT", "8804")))
    p.add_argument("--control-port", type=int,
                   default=int(os.environ.get("NEXUS_AGENT_CONTROL_PORT", "8814")))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    secret_env = args.secret_env or f"{args.node_id.upper()}_SECRET"
    secret = os.environ.get(secret_env)
    if not secret:
        print(f"missing env var {secret_env}", file=sys.stderr)
        return 2
    return asyncio.run(run(
        args.node_id, secret, args.core_url, args.model,
        args.inspector_host, args.inspector_port, args.control_port,
    ))


if __name__ == "__main__":
    sys.exit(main())
