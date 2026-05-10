"""nats_core — NATS-backed equivalent of core/core.py.

Where the original Core was the broker (HTTP+SSE+HMAC+schema+ACL all in one
process), here the broker IS nats-server. This module's only jobs are:

    1. Compile the manifest into a NATS server config
       (one user per node, publish/subscribe permissions derived from edges).
    2. Spawn nats-server with that config (JetStream enabled).
    3. Provision the audit JetStream stream and tail it as a structured log.
    4. Expose helpers so nodes can derive their own credentials.

The big differences vs core/core.py:

    * No /v0/invoke, /v0/respond, /v0/stream — request/reply and pub/sub are
      built into NATS clients.
    * No HMAC — auth is NATS user/password (or NKey if you go further).
    * No edge-list check on each envelope — broker rejects unauthorised
      publish/subscribe at the protocol level.
    * Schema validation is gone from the broker. It MUST live in the node SDK
      (this prototype does it on the responder side, before dispatch).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
from typing import Any

import nats
import yaml

NATS_PORT = int(os.environ.get("NATS_PORT", "4233"))
NATS_HOST = os.environ.get("NATS_HOST", "127.0.0.1")
AUDIT_STREAM = "MESH_AUDIT"
AUDIT_SUBJECT_WILDCARD = "audit.>"


# ---------- subject conventions ----------
#
#   mesh.<from>.<to_node>.<surface>     invocation
#   audit.<from>.<to_node>.<surface>    audit copy (published by responder SDK)
#
# Audit lives on a non-overlapping subject so JetStream doesn't intercept
# request-reply traffic on mesh.> with its own PubAck reply. The SDK
# duplicates each handled message onto audit.<...> so the JS stream still
# captures the full conversation.

def invoke_subject(from_node: str, to_node: str, surface: str) -> str:
    return f"mesh.{from_node}.{to_node}.{surface}"


def listen_subject(node_id: str, surface: str) -> str:
    # The responder doesn't care who the caller is; the broker has already
    # decided the publish was permitted. So the responder subscribes with a
    # wildcard for the from-position.
    return f"mesh.*.{node_id}.{surface}"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ---------- manifest -> nats config ----------

def derive_password(node_id: str) -> str:
    """Deterministic password from node id. In production you'd use NKeys."""
    seed = os.environ.get("MESH_SECRET_SEED", "nats-pivot-seed")
    return hashlib.sha256(f"{seed}:{node_id}".encode()).hexdigest()[:32]


def compile_nats_config(manifest: dict, port: int, jetstream_dir: str) -> str:
    """Translate manifest edges into NATS server config (HOCON-ish).

    Each node becomes a NATS user with:
        publish:   mesh.<self>.<to>.<surface>   for every outgoing edge
        publish:   _INBOX.>                      so it can receive replies
        subscribe: mesh.*.<self>.<surface>      for every surface it owns
        subscribe: _INBOX.>                      for replies routed to it

    The audit role gets read-all on mesh.>.
    """
    nodes = {n["id"]: n for n in manifest.get("nodes", [])}
    edges = manifest.get("relationships", [])

    out_edges: dict[str, list[tuple[str, str]]] = {nid: [] for nid in nodes}
    for rel in edges:
        from_node = rel["from"]
        to_full = rel["to"]
        to_node, surface = to_full.split(".", 1)
        out_edges.setdefault(from_node, []).append((to_node, surface))

    user_blocks = []
    for nid, node in nodes.items():
        pubs = [f"mesh.{nid}.{tn}.{sf}" for tn, sf in out_edges.get(nid, [])]
        pubs.append("_INBOX.>")
        # Every node may write its own audit trail; the audit subject is
        # constrained to the node's own id-prefix so a node can't forge
        # audit lines on behalf of another.
        pubs.append(f"audit.{nid}.>")
        subs = [f"mesh.*.{nid}.{s['name']}" for s in node.get("surfaces", []) or []]
        subs.append("_INBOX.>")
        password = derive_password(nid)
        user_blocks.append(
            f'  {{ user: "{nid}", password: "{password}", '
            f"permissions: {{ "
            f"publish: {{ allow: {json.dumps(pubs)} }}, "
            f"subscribe: {{ allow: {json.dumps(subs)} }} "
            f"}} }}"
        )

    audit_password = derive_password("__audit__")
    user_blocks.append(
        f'  {{ user: "audit", password: "{audit_password}", '
        f'permissions: {{ subscribe: {{ allow: ["audit.>", "$JS.>", "_INBOX.>"] }}, '
        f'publish: {{ allow: ["$JS.>", "_INBOX.>"] }} }} }}'
    )

    cfg = f"""# Auto-generated from manifest. Do not edit.
host: "{NATS_HOST}"
port: {port}
http_port: {port + 1000}
jetstream {{
  store_dir: "{jetstream_dir}"
}}
authorization {{
  users: [
{chr(10).join(user_blocks)}
  ]
}}
"""
    return cfg


# ---------- audit ----------

async def setup_audit_stream(nc) -> None:
    """Create a JetStream stream that mirrors every mesh.* message.

    Replaces the audit.log JSON file: durable, replayable, queryable by subject.
    """
    js = nc.jetstream()
    try:
        await js.stream_info(AUDIT_STREAM)
    except Exception:
        from nats.js.api import StreamConfig, StorageType, RetentionPolicy
        await js.add_stream(StreamConfig(
            name=AUDIT_STREAM,
            subjects=[AUDIT_SUBJECT_WILDCARD],
            storage=StorageType.FILE,
            retention=RetentionPolicy.LIMITS,
            max_msgs=100_000,
        ))


async def tail_audit(nc, out_path: pathlib.Path) -> None:
    """Tail audit.> with a core subscription and write each line as JSONL.

    JetStream durably stores the same messages (see setup_audit_stream) for
    replay; this callback just makes them human-visible.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    async def _on_msg(msg) -> None:
        try:
            payload = json.loads(msg.data.decode())
        except Exception:
            payload = {"raw": msg.data.decode("utf-8", "replace")}
        evt = {"ts": now_iso(), "subject": msg.subject, "payload": payload}
        with out_path.open("a") as f:
            f.write(json.dumps(evt) + "\n")

    await nc.subscribe(AUDIT_SUBJECT_WILDCARD, cb=_on_msg)


# ---------- bootstrap ----------

class NatsBroker:
    def __init__(self, manifest_path: str, work_dir: str, port: int = NATS_PORT):
        self.manifest_path = pathlib.Path(manifest_path).resolve()
        self.work_dir = pathlib.Path(work_dir).resolve()
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.audit_task: asyncio.Task | None = None

    def load_manifest(self) -> dict:
        return yaml.safe_load(self.manifest_path.read_text())

    def write_config(self) -> pathlib.Path:
        m = self.load_manifest()
        js_dir = self.work_dir / "jetstream"
        js_dir.mkdir(parents=True, exist_ok=True)
        cfg = compile_nats_config(m, self.port, str(js_dir))
        cfg_path = self.work_dir / "nats.conf"
        cfg_path.write_text(cfg)
        return cfg_path

    def start_server(self) -> None:
        cfg_path = self.write_config()
        log_path = self.work_dir / "nats-server.log"
        binary = shutil.which("nats-server") or "/opt/homebrew/opt/nats-server/bin/nats-server"
        self.proc = subprocess.Popen(
            [binary, "-c", str(cfg_path)],
            stdout=open(log_path, "ab"),
            stderr=subprocess.STDOUT,
        )

    async def wait_ready(self, timeout: float = 5.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        last_err: Exception | None = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                nc = await nats.connect(
                    f"nats://audit:{derive_password('__audit__')}@"
                    f"{NATS_HOST}:{self.port}",
                    connect_timeout=0.5,
                )
                await nc.close()
                return
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.1)
        raise RuntimeError(f"nats-server did not come up: {last_err}")

    async def start(self) -> None:
        self.start_server()
        await self.wait_ready()
        self.audit_nc = await nats.connect(
            f"nats://audit:{derive_password('__audit__')}@{NATS_HOST}:{self.port}"
        )
        await setup_audit_stream(self.audit_nc)
        audit_log = self.work_dir / "audit.jsonl"
        await tail_audit(self.audit_nc, audit_log)

    async def stop(self) -> None:
        if getattr(self, "audit_nc", None):
            await self.audit_nc.drain()
        if self.proc:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------- node creds export ----------

def node_url(node_id: str, port: int = NATS_PORT) -> str:
    return f"nats://{node_id}:{derive_password(node_id)}@{NATS_HOST}:{port}"


def node_listen_subjects(manifest: dict, node_id: str) -> list[str]:
    for n in manifest.get("nodes", []):
        if n["id"] == node_id:
            return [listen_subject(node_id, s["name"])
                    for s in n.get("surfaces", []) or []]
    return []


# ---------- CLI ----------

async def _amain(manifest: str, work_dir: str, port: int) -> None:
    broker = NatsBroker(manifest, work_dir, port=port)
    await broker.start()
    print(f"[nats_core] nats-server up on {NATS_HOST}:{port}, "
          f"audit -> {broker.work_dir / 'audit.jsonl'}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    await broker.stop()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", default="manifest.yaml")
    p.add_argument("--work-dir", default="run_logs")
    p.add_argument("--port", type=int, default=NATS_PORT)
    args = p.parse_args(argv)
    asyncio.run(_amain(args.manifest, args.work_dir, args.port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
