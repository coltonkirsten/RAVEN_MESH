"""
benchmark.py — cold-spawn vs daemon, real measurements, no synthetic noise.

What we measure
---------------
For each of N iterations of the same surface (echo, weather):

    cold-spawn: time.perf_counter() before SpawnRunner.dispatch(),
                after the response returns. The runner internally
                splits this into spawn_ms (process start) and
                handler_ms (work after stdin closed).

    daemon:     time.perf_counter() right before writing one envelope
                line into the daemon's stdin, right after the matching
                response line is read off stdout. One daemon process
                handles all N envelopes serially.

Memory: ps -o rss=  (resident set size, KB) sampled at three points:
    - daemon idle, just after start, before any envelope
    - daemon active, in the middle of the 100 envelope run
    - cold-spawn child, while it's blocked on stdin (we send the
      envelope after sampling)

Output: results/echo.json, results/weather.json with raw + percentile
summaries. ANALYSIS.md reads from these.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import pathlib
import statistics
import subprocess
import sys
import time
from dataclasses import asdict

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from spawn_runner import SpawnRunner, make_envelope  # noqa: E402

RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)

N = 100
WARMUP = 5  # discarded so disk caches/page caches are warm


def percentile(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    if len(samples) == 1:
        return samples[0]
    s = sorted(samples)
    k = (len(s) - 1) * (p / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def summarize(samples: list[float]) -> dict:
    return {
        "count": len(samples),
        "mean_ms": round(statistics.mean(samples), 3),
        "stdev_ms": round(statistics.stdev(samples), 3) if len(samples) > 1 else 0.0,
        "min_ms": round(min(samples), 3),
        "p50_ms": round(percentile(samples, 50), 3),
        "p95_ms": round(percentile(samples, 95), 3),
        "p99_ms": round(percentile(samples, 99), 3),
        "max_ms": round(max(samples), 3),
    }


def rss_kb(pid: int) -> int:
    try:
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True).strip()
        return int(out)
    except (subprocess.CalledProcessError, ValueError):
        return -1


# ---------------- cold-spawn benchmark ----------------

async def bench_cold_spawn(surface: str, payload_factory) -> dict:
    runner = SpawnRunner.from_default_registry(max_concurrent=1)  # serial for fair compare
    totals: list[float] = []
    spawns: list[float] = []
    handlers: list[float] = []
    rss_samples: list[int] = []

    for i in range(N + WARMUP):
        env = make_envelope(frm="bench", to=surface, payload=payload_factory(i))
        # capture the child's RSS during a few invocations by polling ps
        # while the child is alive. The runner's awaitable is short, so we
        # fire-and-poll with asyncio.create_task.
        if WARMUP <= i < WARMUP + 5:
            t = asyncio.create_task(runner.dispatch(env))
            for _ in range(20):
                if t.done():
                    break
                await asyncio.sleep(0.001)
                # find latest python child of this benchmark
                try:
                    out = subprocess.check_output(
                        ["pgrep", "-f", "cold_handlers"], text=True
                    ).strip().splitlines()
                    for pid in out:
                        rss = rss_kb(int(pid))
                        if rss > 0:
                            rss_samples.append(rss)
                except subprocess.CalledProcessError:
                    pass
            res = await t
        else:
            res = await runner.dispatch(env)

        if i < WARMUP:
            continue
        totals.append(res.total_ms)
        spawns.append(res.spawn_ms)
        handlers.append(res.handler_ms)

    return {
        "surface": surface,
        "mode": "cold_spawn",
        "n": N,
        "warmup_discarded": WARMUP,
        "total_ms": summarize(totals),
        "spawn_ms": summarize(spawns),
        "handler_ms": summarize(handlers),
        "rss_kb_samples": {
            "n": len(rss_samples),
            "min": min(rss_samples) if rss_samples else None,
            "max": max(rss_samples) if rss_samples else None,
            "median": int(statistics.median(rss_samples)) if rss_samples else None,
        },
        "raw_total_ms": [round(x, 3) for x in totals],
    }


# ---------------- daemon benchmark ----------------

def bench_daemon_sync(surface: str, payload_factory, daemon_target: str) -> dict:
    """Spawn one daemon, pipe N envelopes through it, time each round-trip."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "daemon_runner", daemon_target],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    # daemon takes a few ms to import + open the read pipe; small wait stabilizes
    # the first measurement so it's not skewed by readiness racing.
    time.sleep(0.15)

    rss_idle = rss_kb(proc.pid)
    rss_mid = -1
    totals: list[float] = []

    try:
        for i in range(N + WARMUP):
            env = make_envelope(frm="bench", to=surface, payload=payload_factory(i))
            line = json.dumps(env) + "\n"
            t0 = time.perf_counter()
            proc.stdin.write(line)
            proc.stdin.flush()
            response_line = proc.stdout.readline()
            t1 = time.perf_counter()
            if not response_line:
                raise RuntimeError(f"daemon closed stdout at iter {i}; stderr=\n{proc.stderr.read()}")
            if i < WARMUP:
                continue
            if i == N // 2:
                rss_mid = rss_kb(proc.pid)
            totals.append((t1 - t0) * 1000)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    return {
        "surface": surface,
        "mode": "daemon",
        "n": N,
        "warmup_discarded": WARMUP,
        "total_ms": summarize(totals),
        "rss_kb": {"idle_at_start": rss_idle, "mid_run": rss_mid},
        "raw_total_ms": [round(x, 3) for x in totals],
    }


# ---------------- payload factories ----------------

def echo_payload(i: int) -> dict:
    return {"i": i, "msg": "hello world"}


def weather_payload(i: int) -> dict:
    return {"city": ["sf", "nyc", "tokyo", "sydney"][i % 4]}


# ---------------- driver ----------------

async def main() -> None:
    cases = [
        ("echo.invoke",   echo_payload,    "cold_handlers.echo:echo"),
        ("weather.lookup", weather_payload, "cold_handlers.weather:weather"),
    ]
    summary = {"runs": []}
    for surface, factory, daemon_target in cases:
        print(f"=== {surface} ===", flush=True)
        cold = await bench_cold_spawn(surface, factory)
        daemon = bench_daemon_sync(surface, factory, daemon_target)
        report = {
            "surface": surface,
            "cold_spawn": cold,
            "daemon": daemon,
            "ratio_p50": (
                round(cold["total_ms"]["p50_ms"] / daemon["total_ms"]["p50_ms"], 1)
                if daemon["total_ms"]["p50_ms"] > 0 else None
            ),
            "ratio_p99": (
                round(cold["total_ms"]["p99_ms"] / daemon["total_ms"]["p99_ms"], 1)
                if daemon["total_ms"]["p99_ms"] > 0 else None
            ),
        }
        out = RESULTS_DIR / f"{surface.replace('.', '_')}.json"
        out.write_text(json.dumps(report, indent=2))
        summary["runs"].append({
            "surface": surface,
            "cold_p50_ms": cold["total_ms"]["p50_ms"],
            "cold_p95_ms": cold["total_ms"]["p95_ms"],
            "cold_p99_ms": cold["total_ms"]["p99_ms"],
            "daemon_p50_ms": daemon["total_ms"]["p50_ms"],
            "daemon_p95_ms": daemon["total_ms"]["p95_ms"],
            "daemon_p99_ms": daemon["total_ms"]["p99_ms"],
            "ratio_p50": report["ratio_p50"],
            "ratio_p99": report["ratio_p99"],
            "cold_rss_median_kb": cold["rss_kb_samples"]["median"],
            "daemon_rss_idle_kb": daemon["rss_kb"]["idle_at_start"],
            "daemon_rss_mid_kb": daemon["rss_kb"]["mid_run"],
        })
        print(json.dumps(summary["runs"][-1], indent=2), flush=True)

    summary["python"] = sys.version.split()[0]
    summary["platform"] = sys.platform
    summary["cpu_count"] = os.cpu_count()
    (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print("\nwrote results to:", RESULTS_DIR)


if __name__ == "__main__":
    asyncio.run(main())
