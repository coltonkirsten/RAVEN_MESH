"""Manifest-reload latency: hit /v0/admin/reload N times. Measures the cost
of re-parsing the YAML manifest, re-loading per-surface JSON Schemas, and
re-walking the relationship list."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time

import aiohttp


def percentile(s, p):
    if not s: return float("nan")
    k = (len(s) - 1) * p
    f = math.floor(k); c = math.ceil(k)
    if f == c: return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


async def main_async(args):
    url = f"{args.core_url}/v0/admin/reload"
    headers = {"X-Admin-Token": os.environ.get("ADMIN_TOKEN", "")}
    samples = []
    async with aiohttp.ClientSession() as session:
        for _ in range(args.warmup):
            async with session.post(url, headers=headers) as r: await r.read()
        for _ in range(args.iters):
            t0 = time.perf_counter()
            async with session.post(url, headers=headers) as r:
                data = await r.json()
                if r.status != 200:
                    raise RuntimeError(f"reload failed: {r.status} {data}")
            samples.append((time.perf_counter() - t0) * 1000)
    s = sorted(samples)
    out = {
        "n": len(s), "min": s[0], "p50": percentile(s, 0.5),
        "p95": percentile(s, 0.95), "p99": percentile(s, 0.99),
        "max": s[-1], "mean": statistics.fmean(s),
    }
    print(json.dumps({"command": "reload", "stats": out}, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--iters", type=int, default=200)
    p.add_argument("--warmup", type=int, default=20)
    args = p.parse_args()
    asyncio.run(main_async(args))
