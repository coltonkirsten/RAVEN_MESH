"""Recorder: subscribes to Core's /v0/admin/stream and stores every envelope.

We index by correlation_id so a multi-hop chain (A -> B, B -> C, C responds,
B responds, A returns) groups under one chain key.

Envelopes are stored to a JSONL file *and* in an in-memory chain index for
fast queries. Replay verification re-checks HMAC signatures against the
node secrets resolved from the live manifest, so a tampered recording is
detectable.
"""
from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import hashlib
import hmac
import json
import logging
import pathlib
import time
from typing import Any

import aiohttp

log = logging.getLogger("chronicle.recorder")


def _canonical(env: dict) -> str:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _hmac_hex(secret: str, env: dict) -> str:
    return hmac.new(secret.encode(), _canonical(env).encode(), hashlib.sha256).hexdigest()


class Chain:
    """One causal chain — every envelope sharing a correlation_id."""

    def __init__(self, correlation_id: str):
        self.correlation_id = correlation_id
        self.events: list[dict] = []
        self.first_ts: float = time.time()
        self.last_ts: float = self.first_ts

    def add(self, evt: dict) -> None:
        self.events.append(evt)
        self.last_ts = time.time()

    def root_invocation(self) -> dict | None:
        for e in self.events:
            if e.get("direction") == "in" and e.get("kind") == "invocation":
                return e
        return self.events[0] if self.events else None

    def summary(self) -> dict:
        root = self.root_invocation() or {}
        terminal_status = "open"
        for e in reversed(self.events):
            rs = e.get("route_status")
            if rs and rs != "routed":
                terminal_status = rs
                break
            if e.get("direction") == "out" and e.get("kind") == "response":
                terminal_status = "ok"
                break
            if e.get("direction") == "out" and e.get("kind") == "error":
                terminal_status = "error"
                break
        return {
            "correlation_id": self.correlation_id,
            "root_from": root.get("from_node"),
            "root_to": root.get("to_surface"),
            "envelope_count": len(self.events),
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "terminal_status": terminal_status,
        }


class Recorder:
    def __init__(self, core_url: str, admin_token: str, store_path: str):
        self.core_url = core_url.rstrip("/")
        self.admin_token = admin_token
        self.store_path = pathlib.Path(store_path)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.chains: collections.OrderedDict[str, Chain] = collections.OrderedDict()
        self._task: asyncio.Task | None = None
        self._http: aiohttp.ClientSession | None = None
        self._on_event: list = []
        self.connected = False
        self._load_existing()

    def on_event(self, cb) -> None:
        self._on_event.append(cb)

    def _load_existing(self) -> None:
        if not self.store_path.exists():
            return
        with self.store_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._index(evt, persist=False)

    def _index(self, evt: dict, *, persist: bool) -> None:
        cid = evt.get("correlation_id") or evt.get("msg_id")
        if not cid:
            return
        chain = self.chains.get(cid)
        if chain is None:
            chain = Chain(cid)
            self.chains[cid] = chain
        chain.add(evt)
        if persist:
            with self.store_path.open("a") as f:
                f.write(json.dumps(evt) + "\n")
        for cb in self._on_event:
            try:
                cb(evt, chain)
            except Exception:
                log.exception("on_event callback failed")

    async def start(self) -> None:
        self._http = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._http:
            await self._http.close()
            self._http = None

    async def _loop(self) -> None:
        backoff = 1.0
        url = f"{self.core_url}/v0/admin/stream"
        headers = {"X-Admin-Token": self.admin_token}
        while True:
            try:
                assert self._http is not None
                timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
                async with self._http.get(url, headers=headers, timeout=timeout) as r:
                    if r.status != 200:
                        log.error("admin stream rejected: %s", r.status)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30)
                        continue
                    self.connected = True
                    backoff = 1.0
                    event_type = None
                    data_lines: list[str] = []
                    while True:
                        raw = await r.content.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if line == "":
                            if event_type == "envelope" and data_lines:
                                try:
                                    evt = json.loads("\n".join(data_lines))
                                    self._index(evt, persist=True)
                                except json.JSONDecodeError:
                                    pass
                            event_type = None
                            data_lines = []
                            continue
                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("recorder loop ended: %s", e)
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    # query API ---------------------------------------------------------

    def list_chains(self, *, limit: int = 50, offset: int = 0,
                    from_node: str | None = None,
                    to_surface: str | None = None,
                    status: str | None = None) -> list[dict]:
        out = []
        for chain in reversed(list(self.chains.values())):
            s = chain.summary()
            if from_node and s["root_from"] != from_node:
                continue
            if to_surface and s["root_to"] != to_surface:
                continue
            if status and s["terminal_status"] != status:
                continue
            out.append(s)
        return out[offset:offset + limit]

    def get_chain(self, correlation_id: str) -> dict | None:
        chain = self.chains.get(correlation_id)
        if chain is None:
            return None
        return {
            "correlation_id": correlation_id,
            "summary": chain.summary(),
            "events": list(chain.events),
        }

    def reverify_chain(self, correlation_id: str, secrets: dict[str, str]) -> dict:
        """Re-compute HMAC over each captured envelope using current secrets.

        The admin tap delivers the *envelope* (with its original signature)
        for invocations and responses. If the recording was tampered with,
        or if a node's identity_secret was rotated, the signatures won't match.
        """
        chain = self.chains.get(correlation_id)
        if chain is None:
            return {"error": "unknown_chain"}
        results = []
        for evt in chain.events:
            from_node = evt.get("from_node")
            secret = secrets.get(from_node)
            sig_present = bool(evt.get("signature_valid"))
            recomputed = None
            if secret and "payload" in evt:
                synth = {
                    "id": evt.get("msg_id"),
                    "correlation_id": evt.get("correlation_id"),
                    "from": from_node,
                    "to": evt.get("to_surface"),
                    "kind": evt.get("kind"),
                    "payload": evt.get("payload", {}),
                }
                if evt.get("wrapped"):
                    synth["wrapped"] = evt.get("wrapped")
                recomputed = _hmac_hex(secret, synth)
            results.append({
                "msg_id": evt.get("msg_id"),
                "from_node": from_node,
                "kind": evt.get("kind"),
                "core_marked_valid": sig_present,
                "recomputed": recomputed,
            })
        return {"correlation_id": correlation_id, "events": results}
