"""nats_node_sdk — equivalent of node_sdk for the NATS-pivot world.

What used to be:
    node = MeshNode(node_id, secret, core_url)
    node.on("ping", handler)
    await node.start()

becomes:
    node = NatsNode(node_id, manifest_path)
    node.on("ping", handler)
    await node.start()

Key differences from node_sdk/__init__.py:

    * No HMAC signing. Auth is the NATS user/password (already in node_url()).
    * No /v0/register handshake — connecting to NATS *is* the registration;
      the broker's permissions are derived from the manifest at boot.
    * No SSE stream loop — handlers are NATS subscriptions.
    * invoke() uses NATS request/reply with reply-inbox for correlation.
    * Schema validation runs on the responder before dispatch.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import pathlib
from typing import Any, Awaitable, Callable

import nats
import yaml
from jsonschema import ValidationError, validate as jsonschema_validate

from nats_core import (
    invoke_subject,
    listen_subject,
    node_url,
)

log = logging.getLogger("mesh.nats_node")
Handler = Callable[[dict], Awaitable[Any]]


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class MeshDeny(Exception):
    def __init__(self, reason: str, **details: Any):
        self.reason = reason
        self.details = details
        super().__init__(reason)


class MeshError(Exception):
    pass


def _load_schemas(manifest_path: pathlib.Path, node_id: str) -> dict[str, dict]:
    manifest = yaml.safe_load(manifest_path.read_text())
    base = manifest_path.parent
    out: dict[str, dict] = {}
    for n in manifest.get("nodes", []):
        if n["id"] != node_id:
            continue
        for s in n.get("surfaces", []) or []:
            schema = json.loads((base / s["schema"]).read_text())
            out[s["name"]] = schema
    return out


class NatsNode:
    def __init__(
        self,
        node_id: str,
        manifest_path: str | pathlib.Path,
        *,
        port: int | None = None,
        invoke_timeout: float = 30.0,
    ):
        self.node_id = node_id
        self.manifest_path = pathlib.Path(manifest_path).resolve()
        self.port = port or int(os.environ.get("NATS_PORT", "4233"))
        self.invoke_timeout = invoke_timeout
        self.handlers: dict[str, Handler] = {}
        self.schemas = _load_schemas(self.manifest_path, node_id)
        self.nc = None
        self._subs: list = []

    def on(self, surface: str, handler: Handler) -> None:
        self.handlers[surface] = handler

    async def start(self) -> None:
        self.nc = await nats.connect(node_url(self.node_id, port=self.port))
        for surface_name in self.schemas.keys():
            sub = await self.nc.subscribe(
                listen_subject(self.node_id, surface_name),
                cb=self._make_dispatch(surface_name),
            )
            self._subs.append(sub)
        log.info("[%s] connected, %d surface(s) subscribed",
                 self.node_id, len(self.schemas))

    async def stop(self) -> None:
        if self.nc:
            await self.nc.drain()
            self.nc = None

    def _make_dispatch(self, surface_name: str):
        schema = self.schemas[surface_name]

        async def _dispatch(msg) -> None:
            handler = self.handlers.get(surface_name)
            try:
                env = json.loads(msg.data.decode())
            except Exception as e:
                await self._reply_error(msg, "bad_json", str(e))
                return
            payload = env.get("payload", {})
            try:
                jsonschema_validate(payload, schema)
            except ValidationError as e:
                await self._reply_error(msg, "denied_schema_invalid", str(e))
                await self._audit(env, surface_name, "denied_schema_invalid")
                return
            if not handler:
                await self._reply_error(msg, "no_handler", surface_name)
                return
            try:
                result = await handler(env)
            except MeshDeny as d:
                await self._reply_error(msg, d.reason, json.dumps(d.details))
                await self._audit(env, surface_name, f"denied_{d.reason}")
                return
            except Exception as e:
                log.exception("[%s] handler raised", self.node_id)
                await self._reply_error(msg, "handler_exception", str(e))
                await self._audit(env, surface_name, "handler_exception")
                return
            if msg.reply and result is not None:
                resp = {
                    "kind": "response",
                    "from": self.node_id,
                    "ts": now_iso(),
                    "payload": result,
                }
                await self.nc.publish(msg.reply, json.dumps(resp).encode())
            await self._audit(env, surface_name, "routed")

        return _dispatch

    async def _audit(self, env: dict, surface_name: str, decision: str) -> None:
        # audit.<self>.<from>.<surface>.<decision> — every responder writes
        # its own audit line. Audit user has subscribe on audit.> and the
        # JetStream stream MESH_AUDIT captures it.
        from_node = env.get("from", "?")
        subject = f"audit.{self.node_id}.{from_node}.{surface_name}.{decision}"
        body = json.dumps({
            "decision": decision,
            "from": from_node,
            "to": env.get("to"),
            "ts": now_iso(),
            "payload_keys": list(env.get("payload", {}).keys()),
        }).encode()
        try:
            await self.nc.publish(subject, body)
        except Exception as e:
            log.warning("[%s] audit publish failed: %s", self.node_id, e)

    async def _reply_error(self, msg, reason: str, details: str) -> None:
        if not msg.reply:
            return
        body = {
            "kind": "error",
            "from": self.node_id,
            "ts": now_iso(),
            "payload": {"reason": reason, "details": details},
        }
        await self.nc.publish(msg.reply, json.dumps(body).encode())

    async def invoke(self, target_surface: str, payload: dict,
                     *, wait: bool = True, timeout: float | None = None) -> dict:
        if "." not in target_surface:
            raise ValueError("target must be 'node.surface'")
        to_node, surface = target_surface.split(".", 1)
        subj = invoke_subject(self.node_id, to_node, surface)
        env = {
            "kind": "invocation",
            "from": self.node_id,
            "to": target_surface,
            "ts": now_iso(),
            "payload": payload,
        }
        body = json.dumps(env).encode()
        if not wait:
            await self.nc.publish(subj, body)
            return {"status": "accepted"}
        t = timeout if timeout is not None else self.invoke_timeout
        try:
            msg = await self.nc.request(subj, body, timeout=t)
        except nats.errors.NoRespondersError:
            raise MeshError(f"no_responders for {target_surface}")
        except asyncio.TimeoutError:
            raise MeshError(f"timeout invoking {target_surface}")
        except Exception as e:
            # Permission violations show up as a publish error from the
            # underlying TCP stream (subject is silently dropped). Re-raise
            # as MeshError to keep the surface clean.
            raise MeshError(f"invoke_failed: {e}") from e
        try:
            return json.loads(msg.data.decode())
        except Exception:
            raise MeshError("bad reply payload")
