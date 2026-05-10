"""Bench: invoke latency for mesh-direct-HTTP vs mesh-on-NATS.

Setup:
    HTTP path  — repo's core/core.py + node_sdk on :8765, with the
                 dummy_capability echoing on `echo.ping`. The bench process
                 connects as `dashboard` and times 100 invokes of echo.ping.
    NATS path  — this experiment's nats_core + echo_node on :4244. The bench
                 process connects as `dashboard` and times 100 invokes.

Output: p50/p95/p99 + a JSON sidecar in run_logs/bench.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import nats  # noqa: E402
from node_sdk import MeshNode  # noqa: E402  (repo SDK)
from nats_core import NatsBroker, derive_password, node_url  # noqa: E402
from nats_node_sdk import NatsNode  # noqa: E402

N = int(os.environ.get("BENCH_N", "100"))
HTTP_PORT = int(os.environ.get("BENCH_HTTP_PORT", "8765"))
NATS_PORT = int(os.environ.get("BENCH_NATS_PORT", "4244"))
WORK = HERE / "run_logs"
WORK.mkdir(exist_ok=True)


# ---------- helpers ----------

def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[idx]


def stats(samples: list[float]) -> dict:
    return {
        "n": len(samples),
        "p50_ms": round(percentile(samples, 50) * 1000, 3),
        "p95_ms": round(percentile(samples, 95) * 1000, 3),
        "p99_ms": round(percentile(samples, 99) * 1000, 3),
        "min_ms": round(min(samples) * 1000, 3),
        "max_ms": round(max(samples) * 1000, 3),
        "mean_ms": round(sum(samples) / len(samples) * 1000, 3),
    }


# ---------- HTTP path ----------

async def bench_http() -> dict:
    secrets = {
        "BENCH_ECHO_SECRET": "echo-secret-deadbeef",
        "BENCH_DASHBOARD_SECRET": "dash-secret-deadbeef",
    }
    env = {**os.environ, **secrets,
           "MESH_PORT": str(HTTP_PORT),
           "MESH_HOST": "127.0.0.1",
           "AUDIT_LOG": str(WORK / "http_audit.log"),
           "PYTHONUNBUFFERED": "1"}

    core_log = open(WORK / "http_core.log", "ab")
    core = subprocess.Popen(
        [sys.executable, "-m", "core.core",
         "--manifest", str(HERE / "bench_manifest.yaml"),
         "--port", str(HTTP_PORT)],
        cwd=str(REPO), env=env,
        stdout=core_log, stderr=subprocess.STDOUT,
    )
    # wait for core to be ready
    import urllib.request as ur
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            ur.urlopen(f"http://127.0.0.1:{HTTP_PORT}/v0/healthz", timeout=0.5).read()
            break
        except Exception:
            await asyncio.sleep(0.1)
    else:
        core.kill()
        core_log.close()
        raise RuntimeError("HTTP core did not start")

    echo_log = open(WORK / "http_echo.log", "ab")
    echo = subprocess.Popen(
        [sys.executable, "-m", "nodes.dummy.dummy_capability",
         "--node-id", "echo", "--secret-env", "BENCH_ECHO_SECRET",
         "--core-url", f"http://127.0.0.1:{HTTP_PORT}"],
        cwd=str(REPO), env=env,
        stdout=echo_log, stderr=subprocess.STDOUT,
    )
    await asyncio.sleep(0.8)

    samples: list[float] = []
    try:
        node = MeshNode(
            node_id="dashboard",
            secret=secrets["BENCH_DASHBOARD_SECRET"],
            core_url=f"http://127.0.0.1:{HTTP_PORT}",
        )
        await node.start()

        for _ in range(N):
            t0 = time.perf_counter()
            r = await node.invoke("echo.ping", {"text": "hi"})
            samples.append(time.perf_counter() - t0)
            assert r.get("payload", {}).get("echo", {}).get("text") == "hi", r

        await node.stop()
    finally:
        for p in (echo, core):
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        echo_log.close()
        core_log.close()

    return stats(samples)


# ---------- NATS path ----------

async def bench_nats() -> dict:
    broker = NatsBroker(
        manifest_path=str(HERE / "manifest.yaml"),
        work_dir=str(WORK),
        port=NATS_PORT,
    )
    await broker.start()

    py = sys.executable
    env = {**os.environ, "NATS_PORT": str(NATS_PORT),
           "PYTHONUNBUFFERED": "1"}
    echo_log = open(WORK / "nats_echo.log", "ab")
    echo = subprocess.Popen(
        [py, str(HERE / "nodes" / "echo_node.py")],
        env=env, stdout=echo_log, stderr=subprocess.STDOUT,
    )
    await asyncio.sleep(0.6)

    samples: list[float] = []
    try:
        node = NatsNode("dashboard", str(HERE / "manifest.yaml"), port=NATS_PORT)
        await node.start()
        for _ in range(N):
            t0 = time.perf_counter()
            r = await node.invoke("echo.ping", {"text": "hi"})
            samples.append(time.perf_counter() - t0)
            assert r.get("payload", {}).get("echo") == "hi", r
        await node.stop()
    finally:
        echo.send_signal(signal.SIGTERM)
        try:
            echo.wait(timeout=3)
        except subprocess.TimeoutExpired:
            echo.kill()
        echo_log.close()
        await broker.stop()

    return stats(samples)


# ---------- main ----------

async def main() -> None:
    print(f"[bench] N={N}", flush=True)
    print("[bench] running HTTP path...", flush=True)
    http_stats = await bench_http()
    print("  http:", json.dumps(http_stats), flush=True)
    await asyncio.sleep(0.3)
    print("[bench] running NATS path...", flush=True)
    nats_stats = await bench_nats()
    print("  nats:", json.dumps(nats_stats), flush=True)

    out = {"N": N, "http": http_stats, "nats": nats_stats}
    (WORK / "bench.json").write_text(json.dumps(out, indent=2))
    print(f"[bench] wrote {WORK / 'bench.json'}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
