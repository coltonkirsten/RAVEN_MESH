"""replay_node — provenance replay capability for RAVEN_MESH.

What it does
------------
Subscribes to the running Core's envelope tap (``/v0/admin/stream``), captures
every routed envelope into an in-memory + on-disk store keyed by
``correlation_id``, and serves four mesh surfaces that let any peer rewind,
re-execute, and A/B mutate past invocations:

    list({since?, to_surface?, from_node?, limit?})
        -> chains: list of {correlation_id, started_at, first_from, first_to,
                            invocation_count}
    chain({correlation_id})
        -> envelopes: ordered list of every captured envelope sharing that id
    run({correlation_id, dry_run?, mutate?})
        -> replay_correlation_id: a new id that the re-fired envelopes carry
           results: ordered list of {to_surface, status, response} from
                    /v0/admin/invoke for each invocation in the chain
    diff({left_correlation_id, right_correlation_id})
        -> rows: aligned per-position {to_surface, left_response,
                  right_response, equal: bool}

Why this is mesh-only
---------------------
Every envelope routed through Core is signed, schema-validated, and visible
to admin taps with its full payload. The audit log gives correlation chains
for free. ``/v0/admin/invoke`` lets a peer synthesize a new signed envelope
*from* any registered node — so we can replay a captured invocation as if the
original sender had just sent it. Outside the mesh, "replay yesterday's
request against today's services" needs per-service idempotency keys, an
event-sourcing pipeline, and bespoke replay tooling. Here it is one node.

Layer
-----
This is **opinionated-layer** code. The protocol-layer primitives it relies
on (signed envelopes, ``correlation_id``, the admin stream + admin/invoke
endpoints, schema-typed surfaces) are unchanged. A different product could
ignore replay entirely, or wire its own replayer; nothing about this file
escapes into core/.

The fact that ``replay_node`` reads ``/v0/admin/stream`` (with an
``ADMIN_TOKEN``) is a known wart that the v1 PRD addresses with HR-15:
``core.audit_stream`` becomes a normal mesh surface. When that lands, the
``ADMIN_TOKEN`` dependency in this file collapses to a normal manifest edge.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import pathlib
import signal
import sys
from collections import OrderedDict
from typing import Any

import aiohttp

from node_sdk import MeshNode, MeshDeny


# ----------------------------- capture store ------------------------------


class CaptureStore:
    """Append-only in-memory + JSONL store of captured envelopes.

    Indexed by ``correlation_id``. Insertion order preserved. Persists to disk
    so the demo can crash + recover without losing the demo's history.
    """

    def __init__(self, persist_path: pathlib.Path | None = None) -> None:
        self._chains: "OrderedDict[str, list[dict]]" = OrderedDict()
        self._envelopes_by_msg_id: dict[str, dict] = {}
        self._persist_path = persist_path
        if persist_path and persist_path.exists():
            self._load_persist()

    def _load_persist(self) -> None:
        assert self._persist_path is not None
        for line in self._persist_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._index(evt, persist=False)

    def _index(self, evt: dict, *, persist: bool) -> None:
        cid = evt.get("correlation_id")
        msg_id = evt.get("msg_id")
        if not cid:
            return
        if msg_id and msg_id in self._envelopes_by_msg_id:
            return  # admin tap replays history on connect; dedup
        self._chains.setdefault(cid, []).append(evt)
        if msg_id:
            self._envelopes_by_msg_id[msg_id] = evt
        if persist and self._persist_path is not None:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._persist_path, "a") as f:
                f.write(json.dumps(evt) + "\n")

    def add(self, evt: dict) -> None:
        self._index(evt, persist=True)

    def chain(self, cid: str) -> list[dict]:
        return list(self._chains.get(cid, []))

    def list_chains(
        self,
        *,
        since: str | None = None,
        to_surface: str | None = None,
        from_node: str | None = None,
        limit: int = 0,
    ) -> list[dict]:
        out: list[dict] = []
        for cid, envs in self._chains.items():
            if not envs:
                continue
            invocations = [e for e in envs if e.get("kind") == "invocation"]
            if not invocations:
                continue
            first = invocations[0]
            if since and first.get("ts", "") < since:
                continue
            if to_surface and first.get("to_surface") != to_surface:
                continue
            if from_node and first.get("from_node") != from_node:
                continue
            out.append({
                "correlation_id": cid,
                "started_at": first.get("ts"),
                "first_from": first.get("from_node"),
                "first_to": first.get("to_surface"),
                "invocation_count": len(invocations),
                "envelope_count": len(envs),
            })
        if limit > 0:
            out = out[-limit:]
        return out

    def __len__(self) -> int:  # pragma: no cover - convenience
        return sum(len(v) for v in self._chains.values())


# ------------------------- admin-stream subscriber ------------------------


class AdminStreamSubscriber:
    """Tail Core's /v0/admin/stream and feed each envelope into a callback.

    Survives a connection drop with exponential backoff. Logs to stderr.
    """

    def __init__(self, core_url: str, admin_token: str, on_envelope) -> None:
        self.core_url = core_url.rstrip("/")
        self.admin_token = admin_token
        self.on_envelope = on_envelope
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        backoff = 1.0
        url = f"{self.core_url}/v0/admin/stream"
        headers = {"X-Admin-Token": self.admin_token}
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
                    async with session.get(url, headers=headers, timeout=timeout) as r:
                        if r.status != 200:
                            print(f"[replay_node] admin/stream rejected: {r.status}",
                                  file=sys.stderr, flush=True)
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2, 30.0)
                            continue
                        backoff = 1.0
                        await self._consume(r)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[replay_node] admin/stream error: {e}",
                      file=sys.stderr, flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _consume(self, response) -> None:
        event_type: str | None = None
        data_lines: list[str] = []
        while not self._stop.is_set():
            raw = await response.content.readline()
            if not raw:
                return
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                if event_type and data_lines:
                    data_str = "\n".join(data_lines)
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        data = None
                    if event_type == "envelope" and isinstance(data, dict):
                        try:
                            self.on_envelope(data)
                        except Exception as e:  # pragma: no cover
                            print(f"[replay_node] on_envelope raised: {e}",
                                  file=sys.stderr, flush=True)
                event_type = None
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())


# --------------------------------- replayer -------------------------------


def apply_mutation(payload: dict, mutate: dict | None, to_surface: str) -> dict:
    """Return a (possibly mutated) copy of ``payload``.

    Shallow merges ``mutate.set`` into ``payload`` if ``mutate.to_surface``
    is unset or matches. Deep-copies the payload so we don't disturb stored
    envelopes.
    """
    out = copy.deepcopy(payload) if isinstance(payload, dict) else payload
    if not mutate:
        return out
    target = mutate.get("to_surface")
    if target and target != to_surface:
        return out
    setblock = mutate.get("set", {})
    if isinstance(out, dict) and isinstance(setblock, dict):
        out.update(setblock)
    return out


class Replayer:
    """Re-fires invocations from a captured chain via /v0/admin/invoke."""

    def __init__(self, core_url: str, admin_token: str) -> None:
        self.core_url = core_url.rstrip("/")
        self.admin_token = admin_token

    async def run(
        self,
        chain: list[dict],
        *,
        dry_run: bool,
        mutate: dict | None,
    ) -> tuple[str, list[dict]]:
        """Re-fire every invocation envelope in ``chain``. Returns
        (replay_correlation_id, results).

        The new envelopes don't share the original ``correlation_id`` because
        ``/v0/admin/invoke`` mints a fresh id per call. We tag the
        ``replay_correlation_id`` field of each result with the cid of the
        first re-fire so callers can later ``chain(replay_correlation_id)`` to
        compare against the original.
        """
        invocations = [e for e in chain if e.get("kind") == "invocation"]
        if not invocations:
            return "", []
        results: list[dict] = []
        replay_cid: str | None = None
        async with aiohttp.ClientSession() as session:
            for env in invocations:
                from_node = env.get("from_node")
                to_surface = env.get("to_surface")
                payload = env.get("payload") or {}
                payload_to_send = apply_mutation(payload, mutate, to_surface)
                step = {
                    "from_node": from_node,
                    "to_surface": to_surface,
                    "original_msg_id": env.get("msg_id"),
                    "original_payload": payload,
                    "sent_payload": payload_to_send,
                }
                if dry_run:
                    step["dry_run"] = True
                    results.append(step)
                    continue
                body = {
                    "from_node": from_node,
                    "target": to_surface,
                    "payload": payload_to_send,
                }
                try:
                    async with session.post(
                        f"{self.core_url}/v0/admin/invoke",
                        json=body,
                        headers={"X-Admin-Token": self.admin_token},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        step["status"] = r.status
                        try:
                            step["response"] = await r.json()
                        except Exception:
                            step["response"] = {"raw": await r.text()}
                except Exception as e:
                    step["status"] = -1
                    step["response"] = {"error": str(e)}
                results.append(step)
                if replay_cid is None:
                    resp = step.get("response") or {}
                    if isinstance(resp, dict):
                        cid = resp.get("correlation_id")
                        if isinstance(cid, str):
                            replay_cid = cid
        return (replay_cid or ""), results


# --------------------------------- handlers -------------------------------


def make_handlers(store: CaptureStore, replayer: Replayer):
    async def on_list(env: dict) -> dict:
        p = env.get("payload", {}) or {}
        return {
            "chains": store.list_chains(
                since=p.get("since"),
                to_surface=p.get("to_surface"),
                from_node=p.get("from_node"),
                limit=int(p.get("limit", 0)),
            ),
            "total_envelopes": len(store),
        }

    async def on_chain(env: dict) -> dict:
        p = env.get("payload", {}) or {}
        cid = p.get("correlation_id")
        if not isinstance(cid, str) or not cid:
            raise MeshDeny("missing_correlation_id")
        envelopes = store.chain(cid)
        return {
            "correlation_id": cid,
            "envelopes": envelopes,
            "length": len(envelopes),
        }

    async def on_run(env: dict) -> dict:
        p = env.get("payload", {}) or {}
        cid = p.get("correlation_id")
        if not isinstance(cid, str) or not cid:
            raise MeshDeny("missing_correlation_id")
        chain = store.chain(cid)
        if not chain:
            raise MeshDeny("unknown_correlation_id", correlation_id=cid)
        replay_cid, results = await replayer.run(
            chain,
            dry_run=bool(p.get("dry_run", False)),
            mutate=p.get("mutate"),
        )
        return {
            "source_correlation_id": cid,
            "replay_correlation_id": replay_cid,
            "invocations_replayed": len(results),
            "results": results,
        }

    async def on_diff(env: dict) -> dict:
        p = env.get("payload", {}) or {}
        left_cid = p.get("left_correlation_id")
        right_cid = p.get("right_correlation_id")
        if not (isinstance(left_cid, str) and isinstance(right_cid, str)):
            raise MeshDeny("missing_ids")
        left = [e for e in store.chain(left_cid) if e.get("kind") == "response"]
        right = [e for e in store.chain(right_cid) if e.get("kind") == "response"]
        rows: list[dict] = []
        for i in range(max(len(left), len(right))):
            le = left[i] if i < len(left) else None
            re = right[i] if i < len(right) else None
            row = {
                "step": i,
                "to_surface": (le or re or {}).get("from_node"),
                "left_response": (le or {}).get("payload") if le else None,
                "right_response": (re or {}).get("payload") if re else None,
            }
            row["equal"] = row["left_response"] == row["right_response"]
            rows.append(row)
        return {
            "left_correlation_id": left_cid,
            "right_correlation_id": right_cid,
            "rows": rows,
            "all_equal": all(r["equal"] for r in rows) if rows else None,
        }

    return {"list": on_list, "chain": on_chain, "run": on_run, "diff": on_diff}


# ----------------------------------- main ---------------------------------


async def run(
    node_id: str,
    secret: str,
    admin_token: str,
    core_url: str,
    capture_path: pathlib.Path | None,
) -> int:
    store = CaptureStore(capture_path)
    replayer = Replayer(core_url, admin_token)
    subscriber = AdminStreamSubscriber(core_url, admin_token, store.add)

    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.connect()
    for name, h in make_handlers(store, replayer).items():
        node.on(name, h)
    await node.serve()
    subscriber.start()

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    print(
        f"[{node_id}] replay_node ready. "
        f"capture_path={capture_path} subscribed=admin/stream",
        flush=True,
    )
    await stop.wait()
    await subscriber.stop()
    await node.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default="replay_node")
    p.add_argument("--secret-env", default="REPLAY_NODE_SECRET")
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--capture", default=os.environ.get("REPLAY_CAPTURE",
                                                       "experiments/mesh_only_top2/replay_node/captures.jsonl"))
    args = p.parse_args()
    secret = os.environ.get(args.secret_env)
    if not secret:
        print(f"missing env var {args.secret_env}", file=sys.stderr)
        return 2
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        print("missing env var ADMIN_TOKEN", file=sys.stderr)
        return 2
    capture_path = pathlib.Path(args.capture).resolve() if args.capture else None
    return asyncio.run(run(args.node_id, secret, admin_token, args.core_url, capture_path))


if __name__ == "__main__":
    sys.exit(main())
