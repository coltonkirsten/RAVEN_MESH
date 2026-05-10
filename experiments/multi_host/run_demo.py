"""End-to-end federation demo.

Boots two Core processes (A on :8000, B on :8001), starts alpha against A
and beta against B, then drives invocations from alpha through Core A's
peer link to Core B's beta and prints the responses. Also exercises the
failure-mode tests:
    * unknown peer (bad PEER_AB_SECRET)
    * replay (resending the same peer envelope)
    * time-skew (manually backdated envelope)
    * forged inner from (claiming to be a remote node not owned by us)

Run from the repo root:
    python -m experiments.multi_host.run_demo

Each subprocess streams its stdout/stderr to the controlling terminal
prefixed with the role name. The script exits 0 if every assertion passes,
non-zero otherwise.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import signal
import sys
import time
import uuid
from typing import Any

import aiohttp

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
HERE = pathlib.Path(__file__).resolve().parent
PYTHON = sys.executable

CORE_A_URL = "http://127.0.0.1:8000"
CORE_B_URL = "http://127.0.0.1:8001"


def _ensure_secrets() -> None:
    """Pre-set demo secrets so both Cores + nodes pick up the same values."""
    defaults = {
        "ALPHA_SECRET": "alpha-demo-secret-please-rotate",
        "BETA_SECRET": "beta-demo-secret-please-rotate",
        # Per-pair peer HMAC. In production, derive from a key-management
        # system or use Ed25519 pubkey auth instead.
        "PEER_AB_SECRET": "peer-AB-demo-secret-please-rotate",
        "ADMIN_TOKEN": "admin-dev-token",
        "AUDIT_LOG": str(HERE / ".audit.log"),
    }
    for k, v in defaults.items():
        os.environ.setdefault(k, v)


async def _spawn(name: str, *args: str, env_overrides: dict[str, str] | None = None) -> asyncio.subprocess.Process:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    proc = await asyncio.create_subprocess_exec(
        PYTHON, *args,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def reader() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            sys.stdout.write(f"[{name}] {line.decode(errors='replace')}")
            sys.stdout.flush()

    asyncio.create_task(reader())
    return proc


async def _wait_health(url: str, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as s:
        while time.monotonic() < deadline:
            try:
                async with s.get(f"{url}/v0/healthz", timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status == 200:
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError(f"healthz never came up: {url}")


async def _wait_node_connected(core_url: str, node_id: str, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as s:
        while time.monotonic() < deadline:
            try:
                async with s.get(f"{core_url}/v0/introspect", timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status == 200:
                        data = await r.json()
                        for n in data.get("nodes", []):
                            if n.get("id") == node_id and n.get("connected"):
                                return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError(f"node {node_id} never connected on {core_url}")


# ---------- assertions ----------


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"  FAIL: {msg}")
        raise AssertionError(msg)
    print(f"  OK  : {msg}")


# ---------- demo body ----------


async def _admin_invoke(session: aiohttp.ClientSession, core_url: str,
                        from_node: str, target: str, payload: dict) -> tuple[int, Any]:
    """Use Core's /v0/admin/invoke to synthesize a signed envelope from
    `from_node` and route it. The remote-target case still goes through the
    federated /v0/invoke wrapper because admin/invoke calls _route_invocation
    directly. We need to invoke through /v0/invoke for the remote path; so
    we'll synthesize the envelope ourselves below using ALPHA_SECRET.
    """
    body = {"from_node": from_node, "target": target, "payload": payload}
    async with session.post(
        f"{core_url}/v0/admin/invoke", json=body,
        headers={"X-Admin-Token": os.environ["ADMIN_TOKEN"]},
    ) as r:
        return r.status, await r.json()


async def _signed_invoke(session: aiohttp.ClientSession, core_url: str,
                         from_node: str, secret: str, target: str,
                         payload: dict) -> tuple[int, Any]:
    """Build a node-signed envelope and POST to /v0/invoke.

    This exercises the FEDERATED path: federated_handle_invoke detects the
    remote target, verifies the signature, and forwards via peer link.
    """
    import datetime as _dt
    import hmac as _hmac
    import hashlib as _hashlib
    msg_id = str(uuid.uuid4())
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    env: dict[str, Any] = {
        "id": msg_id, "correlation_id": msg_id, "from": from_node,
        "to": target, "kind": "invocation", "payload": payload, "timestamp": ts,
    }
    body = json.dumps({k: v for k, v in env.items() if k != "signature"},
                      sort_keys=True, separators=(",", ":"), default=str)
    env["signature"] = _hmac.new(secret.encode(), body.encode(),
                                 _hashlib.sha256).hexdigest()
    async with session.post(f"{core_url}/v0/invoke", json=env,
                            timeout=aiohttp.ClientTimeout(total=15)) as r:
        return r.status, await r.json()


async def _build_peer_envelope(secret: str, peer_from: str, peer_to: str,
                               inner: dict, *, override_ts: str | None = None,
                               nonce: str | None = None) -> dict:
    import datetime as _dt
    import hmac as _hmac
    import hashlib as _hashlib
    env = {
        "peer_from": peer_from, "peer_to": peer_to,
        "nonce": nonce or uuid.uuid4().hex,
        "timestamp": override_ts or _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "inner": inner,
    }
    body = json.dumps({k: v for k, v in env.items() if k != "signature"},
                      sort_keys=True, separators=(",", ":"), default=str)
    env["signature"] = _hmac.new(secret.encode(), body.encode(),
                                 _hashlib.sha256).hexdigest()
    return env


async def run_demo() -> int:
    _ensure_secrets()
    core_a = await _spawn(
        "core-A",
        "-m", "experiments.multi_host.peer_core",
        "--manifest", str(HERE / "manifestA.yaml"),
        "--port", "8000",
    )
    core_b = await _spawn(
        "core-B",
        "-m", "experiments.multi_host.peer_core",
        "--manifest", str(HERE / "manifestB.yaml"),
        "--port", "8001",
    )

    procs = [core_a, core_b]
    nodes_started: list[asyncio.subprocess.Process] = []

    try:
        await _wait_health(CORE_A_URL)
        await _wait_health(CORE_B_URL)
        print("--- both cores up ---")

        alpha_proc = await _spawn(
            "alpha", "-m", "experiments.multi_host.nodes.alpha",
            "--core-url", CORE_A_URL,
        )
        beta_proc = await _spawn(
            "beta", "-m", "experiments.multi_host.nodes.beta",
            "--core-url", CORE_B_URL,
        )
        nodes_started.extend([alpha_proc, beta_proc])

        await _wait_node_connected(CORE_A_URL, "alpha")
        await _wait_node_connected(CORE_B_URL, "beta")
        print("--- alpha + beta both connected ---")

        async with aiohttp.ClientSession() as s:
            # ---- 1. Happy path: alpha @ A invokes beta.ping @ B ------------
            print("\n[test 1] alpha -> beta.ping (federated request/response)")
            status, body = await _signed_invoke(
                s, CORE_A_URL, "alpha", os.environ["ALPHA_SECRET"],
                "beta.ping", {"hello": "world", "n": 42},
            )
            _assert(status == 200, f"happy-path status=200 (got {status} body={body})")
            payload = body.get("payload", {})
            _assert(payload.get("ok") is True, "response.payload.ok is True")
            _assert(payload.get("served_by") == "beta@B", "served_by=beta@B")
            _assert(payload.get("received_from") == "alpha", "received_from=alpha")
            _assert(payload.get("echo", {}).get("hello") == "world", "payload echoed")
            _assert(body.get("from") == "beta", "envelope.from=beta")
            _assert(body.get("kind") == "response", "envelope.kind=response")

            # ---- 2. Slow surface (covers timeout + multi-second wait) ------
            print("\n[test 2] alpha -> beta.slow (1.5s delay)")
            status, body = await _signed_invoke(
                s, CORE_A_URL, "alpha", os.environ["ALPHA_SECRET"],
                "beta.slow", {"delay_seconds": 1.5},
            )
            _assert(status == 200, "slow-path status=200")
            _assert(abs(body.get("payload", {}).get("slept_for", 0) - 1.5) < 0.2,
                    "slept ~1.5s")

            # ---- 3. Bad alpha signature (caught at A) ----------------------
            print("\n[test 3] alpha invokes beta.ping with WRONG secret (must be 401 at A)")
            status, body = await _signed_invoke(
                s, CORE_A_URL, "alpha", "wrong-secret-not-real",
                "beta.ping", {"hello": "should-not-arrive"},
            )
            _assert(status == 401, f"bad-sig at A returns 401 (got {status})")

            # ---- 4. Edge missing (alpha -> beta.absent) --------------------
            print("\n[test 4] alpha invokes beta.absent (no relationship)")
            status, body = await _signed_invoke(
                s, CORE_A_URL, "alpha", os.environ["ALPHA_SECRET"],
                "beta.absent", {},
            )
            _assert(status == 404, f"unknown surface returns 404 (got {status})")

            # ---- 5. Replay protection: send same peer envelope twice -------
            print("\n[test 5] replay: send same peer envelope twice -> second is 409")
            inner_id = str(uuid.uuid4())
            import datetime as _dt
            import hmac as _hmac
            import hashlib as _hashlib
            ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
            inner = {
                "id": inner_id, "correlation_id": inner_id,
                "from": "alpha", "to": "beta.ping", "kind": "invocation",
                "payload": {"replay_test": True}, "timestamp": ts,
            }
            ibody = json.dumps({k: v for k, v in inner.items() if k != "signature"},
                               sort_keys=True, separators=(",", ":"), default=str)
            inner["signature"] = _hmac.new(os.environ["ALPHA_SECRET"].encode(),
                                            ibody.encode(), _hashlib.sha256).hexdigest()
            wrapped = await _build_peer_envelope(
                os.environ["PEER_AB_SECRET"], "A", "B", inner,
            )
            async with s.post(f"{CORE_B_URL}/v0/peer/envelope", json=wrapped) as r:
                first_status = r.status
                first_body = await r.json()
            async with s.post(f"{CORE_B_URL}/v0/peer/envelope", json=wrapped) as r:
                replay_status = r.status
                replay_body = await r.json()
            _assert(first_status == 200, f"first replay-send 200 (got {first_status})")
            _assert(replay_status == 409,
                    f"second replay-send 409 (got {replay_status} body={replay_body})")

            # ---- 6. Time-skew rejection: backdated peer envelope -----------
            print("\n[test 6] time-skew: peer envelope timestamped 1h ago -> 400")
            inner_id2 = str(uuid.uuid4())
            ts2 = _dt.datetime.now(_dt.timezone.utc).isoformat()
            inner2 = {
                "id": inner_id2, "correlation_id": inner_id2,
                "from": "alpha", "to": "beta.ping", "kind": "invocation",
                "payload": {"skew_test": True}, "timestamp": ts2,
            }
            ibody2 = json.dumps({k: v for k, v in inner2.items() if k != "signature"},
                                sort_keys=True, separators=(",", ":"), default=str)
            inner2["signature"] = _hmac.new(os.environ["ALPHA_SECRET"].encode(),
                                             ibody2.encode(), _hashlib.sha256).hexdigest()
            old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)).isoformat()
            wrapped2 = await _build_peer_envelope(
                os.environ["PEER_AB_SECRET"], "A", "B", inner2, override_ts=old_ts,
            )
            async with s.post(f"{CORE_B_URL}/v0/peer/envelope", json=wrapped2) as r:
                skew_status = r.status
                skew_body = await r.json()
            _assert(skew_status == 400, f"skew rejected 400 (got {skew_status} body={skew_body})")
            _assert(skew_body.get("error") == "peer_skew", "error=peer_skew")

            # ---- 7. Forged peer signature ---------------------------------
            print("\n[test 7] forged peer HMAC -> 401")
            inner_id3 = str(uuid.uuid4())
            ts3 = _dt.datetime.now(_dt.timezone.utc).isoformat()
            inner3 = {
                "id": inner_id3, "correlation_id": inner_id3,
                "from": "alpha", "to": "beta.ping", "kind": "invocation",
                "payload": {"forge_test": True}, "timestamp": ts3,
            }
            ibody3 = json.dumps({k: v for k, v in inner3.items() if k != "signature"},
                                sort_keys=True, separators=(",", ":"), default=str)
            inner3["signature"] = _hmac.new(os.environ["ALPHA_SECRET"].encode(),
                                             ibody3.encode(), _hashlib.sha256).hexdigest()
            wrapped3 = await _build_peer_envelope(
                "totally-wrong-peer-secret", "A", "B", inner3,
            )
            async with s.post(f"{CORE_B_URL}/v0/peer/envelope", json=wrapped3) as r:
                forge_status = r.status
                forge_body = await r.json()
            _assert(forge_status == 401, f"forged peer sig 401 (got {forge_status})")

            # ---- 8. Inner-from not owned by claimed peer ------------------
            print("\n[test 8] peer A claims to be sending from `beta` (its own peer) -> 403")
            inner_id4 = str(uuid.uuid4())
            ts4 = _dt.datetime.now(_dt.timezone.utc).isoformat()
            # `beta` is local to B, not a remote_node from A's perspective
            # at B. So A claiming inner.from=beta must be rejected.
            inner4 = {
                "id": inner_id4, "correlation_id": inner_id4,
                "from": "beta", "to": "beta.ping", "kind": "invocation",
                "payload": {"impersonate_test": True}, "timestamp": ts4,
            }
            ibody4 = json.dumps({k: v for k, v in inner4.items() if k != "signature"},
                                sort_keys=True, separators=(",", ":"), default=str)
            # Doesn't matter that the inner sig is junk; B never verifies it
            # for peer-delivered envelopes, so the impersonation check has to
            # catch this.
            inner4["signature"] = "junk"
            wrapped4 = await _build_peer_envelope(
                os.environ["PEER_AB_SECRET"], "A", "B", inner4,
            )
            async with s.post(f"{CORE_B_URL}/v0/peer/envelope", json=wrapped4) as r:
                impersonate_status = r.status
                impersonate_body = await r.json()
            _assert(impersonate_status == 403,
                    f"impersonation 403 (got {impersonate_status} body={impersonate_body})")

            # ---- 9. Tampered inner payload -> outer signature fails -------
            print("\n[test 9] tamper inner payload after signing -> outer HMAC fails")
            inner_id5 = str(uuid.uuid4())
            ts5 = _dt.datetime.now(_dt.timezone.utc).isoformat()
            inner5 = {
                "id": inner_id5, "correlation_id": inner_id5,
                "from": "alpha", "to": "beta.ping", "kind": "invocation",
                "payload": {"original": "value"}, "timestamp": ts5,
            }
            ibody5 = json.dumps({k: v for k, v in inner5.items() if k != "signature"},
                                sort_keys=True, separators=(",", ":"), default=str)
            inner5["signature"] = _hmac.new(os.environ["ALPHA_SECRET"].encode(),
                                             ibody5.encode(), _hashlib.sha256).hexdigest()
            wrapped5 = await _build_peer_envelope(
                os.environ["PEER_AB_SECRET"], "A", "B", inner5,
            )
            # Now tamper.
            wrapped5["inner"]["payload"]["original"] = "TAMPERED"
            async with s.post(f"{CORE_B_URL}/v0/peer/envelope", json=wrapped5) as r:
                tamper_status = r.status
            _assert(tamper_status == 401, f"tamper -> 401 (got {tamper_status})")

            # ---- 10. /v0/peer/info diagnostic -----------------------------
            print("\n[test 10] /v0/peer/info diagnostic on B")
            async with s.get(
                f"{CORE_B_URL}/v0/peer/info",
                headers={"X-Admin-Token": os.environ["ADMIN_TOKEN"]},
            ) as r:
                info_status = r.status
                info_body = await r.json()
            _assert(info_status == 200, "peer/info 200")
            _assert(info_body.get("local_name") == "B", "local_name=B")
            peer_names = {p["name"] for p in info_body.get("peers", [])}
            _assert("A" in peer_names, "peer A listed")
            remote_ids = {n["id"] for n in info_body.get("remote_nodes", [])}
            _assert("alpha" in remote_ids, "remote node alpha listed")

        print("\n=== ALL TESTS PASSED ===")
        return 0
    finally:
        for proc in nodes_started + procs:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGINT)
        for proc in nodes_started + procs:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5)


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run_demo())
    except KeyboardInterrupt:
        return 130
    except AssertionError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
