"""Cron node — schedules invocations against other mesh surfaces.

Hybrid kind. Exposes three tool surfaces:
    cron.set     -> {schedule, target_surface, payload_template} -> {cron_id}
    cron.delete  -> {cron_id}                                    -> {deleted}
    cron.list    -> {}                                           -> {crons: [...]}

Persists schedules to nodes/cron_node/data/crons.json. On schedule fire,
sends a fire-and-forget invocation to the configured target_surface.

Usage:
    CRON_NODE_SECRET=... python3 -m nodes.cron_node.cron_node \\
        --node-id cron_node --core-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import pathlib
import signal
import sys
import uuid

from croniter import croniter

from node_sdk import MeshError, MeshNode

log = logging.getLogger("cron_node")
DATA_DIR = pathlib.Path(__file__).resolve().parent / "data"
DATA_FILE = DATA_DIR / "crons.json"


def _load() -> dict[str, dict]:
    if not DATA_FILE.exists():
        return {}
    try:
        return json.loads(DATA_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save(crons: dict[str, dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(crons, indent=2))
    tmp.replace(DATA_FILE)


class CronNode:
    def __init__(self, node: MeshNode):
        self.node = node
        self.crons: dict[str, dict] = _load()
        self.tasks: dict[str, asyncio.Task] = {}

    async def start_runners(self) -> None:
        for cron_id in list(self.crons):
            self._spawn(cron_id)

    def _spawn(self, cron_id: str) -> None:
        if cron_id in self.tasks and not self.tasks[cron_id].done():
            return
        self.tasks[cron_id] = asyncio.create_task(self._runner(cron_id))

    async def _runner(self, cron_id: str) -> None:
        try:
            while cron_id in self.crons:
                spec = self.crons[cron_id]
                now = _dt.datetime.now()
                try:
                    itr = croniter(spec["schedule"], now, second_at_beginning=True)
                    next_fire = itr.get_next(_dt.datetime)
                except (ValueError, KeyError):
                    log.exception("cron %s has invalid schedule, removing", cron_id)
                    self.crons.pop(cron_id, None)
                    _save(self.crons)
                    return
                wait = (next_fire - now).total_seconds()
                if wait > 0:
                    await asyncio.sleep(wait)
                if cron_id not in self.crons:
                    return
                try:
                    await self.node.invoke(spec["target_surface"], dict(spec["payload_template"]),
                                           wait=False)
                    log.info("cron %s fired -> %s", cron_id, spec["target_surface"])
                except MeshError as e:
                    log.warning("cron %s fire failed: %s %s", cron_id, e.status, e.data)
                except Exception:
                    log.exception("cron %s fire raised", cron_id)
        except asyncio.CancelledError:
            return

    # tool surfaces ------------------------------------------------------

    async def set_cron(self, env: dict) -> dict:
        body = env["payload"]
        try:
            croniter(body["schedule"], second_at_beginning=True)
        except (ValueError, KeyError) as e:
            return {"error": "invalid_schedule", "details": str(e)}
        cron_id = str(uuid.uuid4())
        self.crons[cron_id] = {
            "id": cron_id,
            "schedule": body["schedule"],
            "target_surface": body["target_surface"],
            "payload_template": body.get("payload_template", {}),
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        _save(self.crons)
        self._spawn(cron_id)
        return {"cron_id": cron_id}

    async def delete_cron(self, env: dict) -> dict:
        cron_id = env["payload"]["cron_id"]
        existed = self.crons.pop(cron_id, None) is not None
        _save(self.crons)
        t = self.tasks.pop(cron_id, None)
        if t:
            t.cancel()
        return {"deleted": existed, "cron_id": cron_id}

    async def list_crons(self, env: dict) -> dict:
        return {"crons": list(self.crons.values())}


async def run(node_id: str, secret: str, core_url: str) -> int:
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.connect()
    cron = CronNode(node)
    node.on("set", cron.set_cron)
    node.on("delete", cron.delete_cron)
    node.on("list", cron.list_crons)
    await node.serve()
    await cron.start_runners()

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    print(f"[{node_id}] cron_node ready. {len(cron.crons)} schedule(s) loaded.", flush=True)
    await stop.wait()
    for t in cron.tasks.values():
        t.cancel()
    await node.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default="cron_node")
    p.add_argument("--secret-env", default=None)
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    secret_env = args.secret_env or f"{args.node_id.upper()}_SECRET"
    secret = os.environ.get(secret_env)
    if not secret:
        print(f"missing env var {secret_env}", file=sys.stderr)
        return 2
    return asyncio.run(run(args.node_id, secret, args.core_url))


if __name__ == "__main__":
    sys.exit(main())
