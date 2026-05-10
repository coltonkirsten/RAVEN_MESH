"""Protocol-level micro-benchmark for RAVEN_MESH.

Drives the bench manifest (bench_client -> bench_echo.ping) using the
node_sdk's MeshNode.invoke() loop. Reports latency percentiles, optional
throughput-under-concurrency, and an ASCII histogram.

Usage:
  python3 scripts/bench/python_bench.py latency --iters 5000
  python3 scripts/bench/python_bench.py throughput --concurrency 64 --duration 20
  python3 scripts/bench/python_bench.py payload --bytes 1024 --iters 2000
  python3 scripts/bench/python_bench.py firefoget --iters 5000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from node_sdk import MeshNode  # noqa: E402


def percentile(sorted_xs, p):
    if not sorted_xs:
        return float("nan")
    k = (len(sorted_xs) - 1) * p
    f = math.floor(k); c = math.ceil(k)
    if f == c:
        return sorted_xs[int(k)]
    return sorted_xs[f] + (sorted_xs[c] - sorted_xs[f]) * (k - f)


def args_view(args):
    return {k: v for k, v in vars(args).items() if not callable(v)}


def stats(samples_ms):
    s = sorted(samples_ms)
    return {
        "n": len(s),
        "min": s[0],
        "p50": percentile(s, 0.50),
        "p90": percentile(s, 0.90),
        "p95": percentile(s, 0.95),
        "p99": percentile(s, 0.99),
        "p999": percentile(s, 0.999),
        "max": s[-1],
        "mean": statistics.fmean(s),
        "stdev": statistics.pstdev(s) if len(s) > 1 else 0.0,
    }


def histogram(samples_ms, bins=20, width=60):
    if not samples_ms:
        return ""
    s = sorted(samples_ms)
    lo = s[0]
    hi = percentile(s, 0.99)  # cap at p99 so the long tail doesn't squash the bulk
    if hi <= lo:
        hi = lo + 1e-9
    edges = [lo + (hi - lo) * i / bins for i in range(bins + 1)]
    counts = [0] * bins
    for v in samples_ms:
        if v >= hi:
            counts[-1] += 1
            continue
        idx = int((v - lo) / (hi - lo) * bins)
        if idx < 0: idx = 0
        if idx >= bins: idx = bins - 1
        counts[idx] += 1
    cmax = max(counts) or 1
    out = []
    out.append(f"  histogram (n={len(samples_ms)}, range {lo:.3f}..{hi:.3f}ms, bin width={(hi-lo)/bins*1000:.1f}us, last bin = >=p99 spillover):")
    for i, c in enumerate(counts):
        bar = "#" * int(c / cmax * width)
        edge = edges[i]
        out.append(f"  {edge:7.3f}ms |{bar:<{width}}| {c}")
    return "\n".join(out)


async def make_client(core_url):
    secret = os.environ.get("BENCH_CLIENT_SECRET")
    if not secret:
        print("BENCH_CLIENT_SECRET not set", file=sys.stderr); sys.exit(2)
    node = MeshNode(node_id="bench_client", secret=secret, core_url=core_url)
    await node.start()
    return node


async def cmd_latency(args):
    node = await make_client(args.core_url)
    try:
        # warm up
        for _ in range(args.warmup):
            await node.invoke("bench_echo.ping", {"i": 0})
        samples = []
        t_total_start = time.perf_counter()
        for i in range(args.iters):
            t0 = time.perf_counter()
            await node.invoke("bench_echo.ping", {"i": i})
            samples.append((time.perf_counter() - t0) * 1000)
        total = time.perf_counter() - t_total_start
        s = stats(samples)
        s["throughput_rps"] = args.iters / total
        s["wallclock_s"] = total
        print(json.dumps({"command": "latency", "args": args_view(args), "stats": s}, indent=2))
        print()
        print(histogram(samples))
    finally:
        await node.stop()


async def _worker(node, target, payload, deadline, counters, latencies):
    while time.perf_counter() < deadline:
        t0 = time.perf_counter()
        try:
            await node.invoke(target, payload)
            latencies.append((time.perf_counter() - t0) * 1000)
            counters[0] += 1
        except Exception:
            counters[1] += 1


async def cmd_throughput(args):
    node = await make_client(args.core_url)
    try:
        # warm up
        for _ in range(args.warmup):
            await node.invoke("bench_echo.ping", {"i": 0})
        deadline = time.perf_counter() + args.duration
        counters = [0, 0]
        latencies = []
        payload = {"i": 0}
        tasks = [
            asyncio.create_task(_worker(node, "bench_echo.ping", payload, deadline, counters, latencies))
            for _ in range(args.concurrency)
        ]
        await asyncio.gather(*tasks)
        ok, err = counters
        s = stats(latencies) if latencies else {"n": 0}
        s["throughput_rps"] = ok / args.duration
        s["errors"] = err
        s["concurrency"] = args.concurrency
        print(json.dumps({"command": "throughput", "args": args_view(args), "stats": s}, indent=2))
        print()
        if latencies:
            print(histogram(latencies))
    finally:
        await node.stop()


async def cmd_payload(args):
    node = await make_client(args.core_url)
    try:
        body = "x" * args.bytes
        payload = {"data": body}
        for _ in range(args.warmup):
            await node.invoke("bench_echo.ping", payload)
        samples = []
        t0_total = time.perf_counter()
        for i in range(args.iters):
            t0 = time.perf_counter()
            await node.invoke("bench_echo.ping", payload)
            samples.append((time.perf_counter() - t0) * 1000)
        total = time.perf_counter() - t0_total
        s = stats(samples)
        s["throughput_rps"] = args.iters / total
        s["payload_bytes"] = args.bytes
        print(json.dumps({"command": "payload", "args": args_view(args), "stats": s}, indent=2))
    finally:
        await node.stop()


async def cmd_firefoget(args):
    node = await make_client(args.core_url)
    try:
        for _ in range(args.warmup):
            await node.invoke("bench_echo.ff", {"i": 0}, wait=False)
        samples = []
        t0_total = time.perf_counter()
        for i in range(args.iters):
            t0 = time.perf_counter()
            await node.invoke("bench_echo.ff", {"i": i}, wait=False)
            samples.append((time.perf_counter() - t0) * 1000)
        total = time.perf_counter() - t0_total
        s = stats(samples)
        s["throughput_rps"] = args.iters / total
        print(json.dumps({"command": "firefoget", "args": args_view(args), "stats": s}, indent=2))
        print()
        print(histogram(samples))
    finally:
        await node.stop()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    sub = p.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("latency")
    p1.add_argument("--iters", type=int, default=5000)
    p1.add_argument("--warmup", type=int, default=200)
    p1.set_defaults(func=cmd_latency)

    p2 = sub.add_parser("throughput")
    p2.add_argument("--concurrency", type=int, default=16)
    p2.add_argument("--duration", type=float, default=15.0)
    p2.add_argument("--warmup", type=int, default=200)
    p2.set_defaults(func=cmd_throughput)

    p3 = sub.add_parser("payload")
    p3.add_argument("--bytes", type=int, default=1024)
    p3.add_argument("--iters", type=int, default=1000)
    p3.add_argument("--warmup", type=int, default=100)
    p3.set_defaults(func=cmd_payload)

    p4 = sub.add_parser("firefoget")
    p4.add_argument("--iters", type=int, default=5000)
    p4.add_argument("--warmup", type=int, default=200)
    p4.set_defaults(func=cmd_firefoget)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
