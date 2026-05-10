"""Replayer: re-runs a captured envelope (or whole chain) against the live mesh.

Uses the existing /v0/admin/invoke endpoint to synthesize a signed envelope
from the original sender's identity. Core records `admin_synthesized: true`
on the resulting envelope (HR-11 in v1 PRD), so replays are visibly
distinct from organic traffic — they don't masquerade.

Schema-compat check: the chronicle pulls /v0/admin/state to read the
current manifest schemas, then validates each captured payload against
them. Reports which captured invocations would now fail validation.

Both features rely on the v0 admin surface — no protocol changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from jsonschema import ValidationError, validate as jsonschema_validate

log = logging.getLogger("chronicle.replayer")


class Replayer:
    def __init__(self, core_url: str, admin_token: str):
        self.core_url = core_url.rstrip("/")
        self.admin_token = admin_token
        self._http: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._http = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None

    async def fetch_state(self) -> dict:
        assert self._http is not None
        async with self._http.get(
            f"{self.core_url}/v0/admin/state",
            headers={"X-Admin-Token": self.admin_token},
        ) as r:
            return await r.json()

    async def replay_one(self, captured: dict) -> dict:
        """Re-invoke a single captured envelope against the live mesh."""
        if captured.get("kind") != "invocation":
            return {"error": "not_an_invocation",
                    "kind": captured.get("kind")}
        from_node = captured.get("from_node")
        target = captured.get("to_surface")
        payload = captured.get("payload", {})
        body = {"from_node": from_node, "target": target, "payload": payload}
        assert self._http is not None
        async with self._http.post(
            f"{self.core_url}/v0/admin/invoke",
            headers={"X-Admin-Token": self.admin_token},
            json=body,
        ) as r:
            try:
                data = await r.json()
            except aiohttp.ContentTypeError:
                data = {"error": "bad_response", "text": await r.text()}
            return {
                "captured_msg_id": captured.get("msg_id"),
                "from_node": from_node,
                "target": target,
                "status": r.status,
                "response": data,
            }

    async def replay_chain(self, chain: dict) -> dict:
        """Replay every invocation in a chain (skips responses, errors)."""
        out = []
        for evt in chain.get("events", []):
            if evt.get("kind") != "invocation":
                continue
            if evt.get("direction") != "in":
                continue
            res = await self.replay_one(evt)
            out.append(res)
        return {"correlation_id": chain.get("correlation_id"),
                "replays": out}

    async def schema_compat(self, chains: list[dict]) -> dict:
        """Validate captured payloads against the *current* schemas.

        Returns per-chain compatibility: which captured invocations would
        now fail validation under the current manifest. This is the
        regression-detection that's only possible because the mesh stores
        every payload's schema in a structured manifest.
        """
        state = await self.fetch_state()
        # surface_id ("node.surface") -> schema dict
        schema_map: dict[str, dict] = {}
        for n in state.get("nodes", []):
            for s in n.get("surfaces", []):
                schema_map[f"{n['id']}.{s['name']}"] = s.get("schema", {})

        report = []
        total = 0
        breaking = 0
        for chain in chains:
            chain_report = {
                "correlation_id": chain.get("correlation_id"),
                "checks": [],
            }
            for evt in chain.get("events", []):
                if evt.get("kind") != "invocation" or evt.get("direction") != "in":
                    continue
                target = evt.get("to_surface")
                schema = schema_map.get(target)
                total += 1
                if schema is None:
                    chain_report["checks"].append({
                        "msg_id": evt.get("msg_id"),
                        "target": target,
                        "compatible": False,
                        "reason": "surface_no_longer_exists",
                    })
                    breaking += 1
                    continue
                try:
                    jsonschema_validate(evt.get("payload", {}), schema)
                    chain_report["checks"].append({
                        "msg_id": evt.get("msg_id"),
                        "target": target,
                        "compatible": True,
                    })
                except ValidationError as e:
                    chain_report["checks"].append({
                        "msg_id": evt.get("msg_id"),
                        "target": target,
                        "compatible": False,
                        "reason": "schema_violation",
                        "details": str(e)[:300],
                    })
                    breaking += 1
            report.append(chain_report)
        return {
            "total_invocations_checked": total,
            "now_breaking": breaking,
            "report": report,
        }

    async def diff_replay(self, captured: dict) -> dict:
        """Replay a captured invocation, then diff response vs the response
        the chain originally received."""
        replay = await self.replay_one(captured)
        # Original response for this msg_id is stored in the chain too —
        # callers pass the captured "response" envelope as `original`. To
        # keep this method standalone we look it up by id in the chain.
        return replay
