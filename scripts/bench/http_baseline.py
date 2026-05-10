"""HTTP-level baseline: hit /v0/healthz repeatedly to measure raw aiohttp
server overhead with no protocol routing, no HMAC, no schema validation,
no SSE. Acts as the lower-bound floor.

Substitutes for wrk/hey when those aren't installed.
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

import aiohttp


def percentile(s, p):
    if not s: return float("nan")
    k = (len(s) - 1) * p
    f = math.floor(k); c = math.ceil(k)
    if f == c: return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def stats(samples):
    s = sorted(samples)
    return {
        "n": len(s), "min": s[0], "p50": percentile(s, 0.5),
        "p95": percentile(s, 0.95), "p99": percentile(s, 0.99),
        "max": s[-1], "mean": statistics.fmean(s),
    }


async def worker(session, url, deadline, counters, latencies):
    while time.perf_counter() < deadline:
        t0 = time.perf_counter()
        try:
            async with session.get(url) as r:
                await r.read()
                counters[0] += 1
                latencies.append((time.perf_counter() - t0) * 1000)
        except Exception:
            counters[1] += 1


async def main_async(args):
    url = f"{args.core_url}/v0/healthz"
    async with aiohttp.ClientSession() as session:
        # warmup
        for _ in range(50):
            async with session.get(url) as r: await r.read()

        if args.mode == "serial":
            samples = []
            t0_total = time.perf_counter()
            for _ in range(args.iters):
                t0 = time.perf_counter()
                async with session.get(url) as r: await r.read()
                samples.append((time.perf_counter() - t0) * 1000)
            total = time.perf_counter() - t0_total
            s = stats(samples); s["throughput_rps"] = args.iters / total
            print(json.dumps({"mode": "serial", "stats": s}, indent=2))
        else:
            deadline = time.perf_counter() + args.duration
            counters = [0, 0]; latencies = []
            tasks = [asyncio.create_task(worker(session, url, deadline, counters, latencies))
                     for _ in range(args.concurrency)]
            await asyncio.gather(*tasks)
            ok, err = counters
            s = stats(latencies)
            s["throughput_rps"] = ok / args.duration
            s["errors"] = err
            s["concurrency"] = args.concurrency
            print(json.dumps({"mode": "concurrent", "stats": s}, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--mode", choices=["serial", "concurrent"], default="serial")
    p.add_argument("--iters", type=int, default=5000)
    p.add_argument("--concurrency", type=int, default=64)
    p.add_argument("--duration", type=float, default=10.0)
    args = p.parse_args()
    asyncio.run(main_async(args))
