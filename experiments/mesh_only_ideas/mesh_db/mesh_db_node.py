"""mesh_db_node — serves Core's audit.log as a queryable database via mesh surfaces.

Surfaces:
    - query : filter audit entries by from_node, to_surface, decision, type, since
    - count : group_by from_node | to_surface | decision | type
    - trace : walk every entry sharing a correlation_id (provenance chain)
    - ping  : echo (used to generate audit traffic for the demo)

Mesh-only because: the audit log already records every routed envelope on the
mesh, with signatures verified at Core. Exposing it through a typed mesh
surface means any peer (a voice actor, the kanban node, an LLM agent on
another runtime) can introspect the system using the same protocol it uses
for its business calls — no per-node logging integration, no out-of-band
observability stack.

Usage:
    MESH_DB_NODE_SECRET=... python3 -m experiments.mesh_only_ideas.mesh_db.mesh_db_node \\
        --core-url http://127.0.0.1:8000 --audit-log audit.log
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import signal
import sys
from collections import Counter

from node_sdk import MeshNode


def load_audit(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def matches(entry: dict, where: dict) -> bool:
    for k, v in where.items():
        if k == "since":
            if entry.get("timestamp", "") < v:
                return False
        elif entry.get(k) != v:
            return False
    return True


def query_entries(entries: list[dict], where: dict, limit: int) -> list[dict]:
    matched = [e for e in entries if matches(e, where or {})]
    if limit > 0:
        matched = matched[-limit:]
    return matched


def count_entries(entries: list[dict], group_by: str) -> dict[str, int]:
    return dict(Counter(e.get(group_by) or "<none>" for e in entries))


def trace_entries(entries: list[dict], correlation_id: str) -> list[dict]:
    return [e for e in entries if e.get("correlation_id") == correlation_id]


def make_handlers(audit_path: pathlib.Path):
    async def on_query(env: dict) -> dict:
        p = env.get("payload", {})
        entries = load_audit(audit_path)
        return {
            "matched": query_entries(entries, p.get("where", {}), int(p.get("limit", 0))),
            "audit_path": str(audit_path),
            "total_in_log": len(entries),
        }

    async def on_count(env: dict) -> dict:
        p = env.get("payload", {})
        entries = load_audit(audit_path)
        return {
            "counts": count_entries(entries, p["group_by"]),
            "total_in_log": len(entries),
        }

    async def on_trace(env: dict) -> dict:
        p = env.get("payload", {})
        cid = p["correlation_id"]
        entries = load_audit(audit_path)
        chain = trace_entries(entries, cid)
        return {
            "correlation_id": cid,
            "chain": chain,
            "length": len(chain),
        }

    async def on_ping(env: dict) -> dict:
        return {"pong": env.get("payload", {}), "from": env.get("from")}

    return {"query": on_query, "count": on_count, "trace": on_trace, "ping": on_ping}


async def run(node_id: str, secret: str, core_url: str, audit_path: pathlib.Path) -> int:
    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.connect()
    for name, handler in make_handlers(audit_path).items():
        node.on(name, handler)
    await node.serve()

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    print(f"[{node_id}] mesh_db_node ready. audit_path={audit_path}", flush=True)
    await stop.wait()
    await node.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default="mesh_db_node")
    p.add_argument("--secret-env", default="MESH_DB_NODE_SECRET")
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--audit-log", default=os.environ.get("AUDIT_LOG", "audit.log"))
    args = p.parse_args()
    secret = os.environ.get(args.secret_env)
    if not secret:
        print(f"missing env var {args.secret_env}", file=sys.stderr)
        return 2
    audit_path = pathlib.Path(args.audit_log).resolve()
    return asyncio.run(run(args.node_id, secret, args.core_url, audit_path))


if __name__ == "__main__":
    sys.exit(main())
