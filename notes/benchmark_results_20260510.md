# RAVEN_MESH benchmark results — 2026-05-10

**Author:** Task Agent (commissioned by Colton Kirsten)
**Date executed:** 2026-05-10
**Scope:** **Implementation layer** measurements of the protocol-layer work
done by the *current Python prototype*. These numbers describe one
implementation of the protocol, not the protocol itself. Per
`PROTOCOL_CONSTRAINT.md`, the protocol must remain unopinionated; the
specific aiohttp + asyncio + jsonschema + HMAC SHA-256 choices used here
are an opinionated *implementation* layer that any v1+ rewrite (BEAM,
Go, Rust) is expected to replace. **Tag every cell below as
"impl-layer measurement."**

The intended source — `notes/benchmark_design.md` — was not present in
the repo when this task ran (verified with `ls notes/`). The task brief
specified the broad strokes ("trivial echo nodes for the
protocol-overhead measurements"), so the methodology is pinned inline
below so this run is reproducible later.

---

## TL;DR

| metric (impl-layer) | value |
|---|---|
| protocol round-trip (warm, c=1, 64 B payload) | **p50 0.44 ms / p95 0.48 ms / p99 0.49 ms** |
| protocol round-trip throughput, single client | **~2.2 k req/s** |
| protocol round-trip throughput, c=64 client | **~4.8 k req/s** (Core saturates here) |
| fire-and-forget invoke (c=1) | p50 0.25 ms / p99 1.35 ms / ~3.0 k req/s |
| HTTP `/healthz` floor (c=1) | p50 0.089 ms / ~10.5 k req/s |
| HTTP `/healthz` floor (c=64) | p50 1.82 ms / ~35 k req/s |
| cold boot to first invocation | **~420 ms** wall (≈280 ms is Python interp startup × 3) |
| `/v0/admin/reload` (3 nodes, 4 schemas) | p50 1.11 ms / p99 1.27 ms |
| protocol overhead vs. /healthz | round-trip costs ≈5× a no-op GET; throughput cap ≈7× lower |

**Protocol-layer takeaway.** The wire protocol — HMAC sign + verify on
both sides, JSON-Schema validate, edge ACL check, SSE deliver, response
sign + verify, future fulfilment — costs about **0.35 ms over the raw
HTTP floor** on this hardware. That's roughly 4×–5× a stateless GET and
is the floor any future BEAM/Rust impl should beat by a wide margin. A
single-process Python Core saturates at **~4.8 k routed envelopes/s**;
that is the practical ceiling of *this implementation*, not of the
protocol.

**Real bug found while running the bench.** The Python `node_sdk`'s SSE
reader uses `aiohttp.StreamReader.readline()` with default
`max_line_size=131072`. Any response payload + envelope overhead that
crosses ~128 KB silently disconnects the receiving node. Tagged
**impl-layer bug**, fix in `node_sdk/__init__.py:_stream_loop` (pass a
larger `read_bufsize` or chunk the SSE delivery on the Core side). This
was never the protocol's fault — the wire protocol does not specify a
delivery transport.

---

## Methodology (pin)

### Layer tagging

Per `PROTOCOL_CONSTRAINT.md`:

- **Protocol layer**: HMAC envelope signing, schema validation, edge ACL,
  request/response semantics, fire-and-forget semantics, manifest reload
  semantics. *What we are trying to characterise.*
- **Implementation layer**: aiohttp server, SSE delivery transport,
  Python `hmac` + `jsonschema` libraries, single asyncio event loop in
  one process. *What we are actually measuring.*

Every number in this doc is an **impl-layer** measurement. To
characterise the *protocol* invariantly we'd need at least a second
implementation; results here are an upper bound on overhead any future
impl must improve on.

### Bench rig (kept trivial on purpose)

- `manifests/bench.yaml` (newly added) — 2 nodes:
  - `bench_client`, an *actor* with a fire-and-forget inbox.
  - `bench_echo`, a *capability* with three surfaces (`ping`, `pong`,
    `ff`) all using the permissive `schemas/echo.json`.
  - 3 declared edges: `bench_client → bench_echo.{ping,pong,ff}`.
- The echo node is `nodes/dummy/dummy_capability.py` (an existing
  generic echo handler — no business logic, just `return {"echo":
  payload, ...}`). This isolates the protocol from any node logic.
- Driver: `scripts/bench/python_bench.py` (newly added) — uses the
  same `node_sdk.MeshNode` that every Python node uses.
- HTTP floor: `scripts/bench/http_baseline.py` (curl-style aiohttp loop
  against `/v0/healthz`).
- Reload: `scripts/bench/reload_bench.py` against `/v0/admin/reload`.
- Boot: `scripts/bench/boot_bench.sh` measures Python-spawn → first
  invocation roundtrip.

`wrk` and `hey` are not installed on this host, so the HTTP-floor
substitute is an aiohttp client in a tight async loop. This is
acceptable because the goal is *relative* comparison (protocol
roundtrip vs. raw HTTP roundtrip on the same loopback, same client lib),
not absolute HTTP throughput numbers.

### Host

| field | value |
|---|---|
| machine | Apple Mac mini, M4, 10 cores (10 perf), 16 GB RAM |
| OS | macOS 26.2 (Darwin 25.2.0, arm64) |
| Python | 3.12.12 (Homebrew) |
| aiohttp | 3.13.5 |
| jsonschema | 4.26.0 |
| pyyaml | 6.0.1 |
| network | loopback (`127.0.0.1`) only |
| Core process | single asyncio loop, no supervisor (`--supervisor` off) |
| audit log | `audit_bench.log` (not flushed between sub-benches) |
| admin rate-limit | `MESH_ADMIN_RATE_LIMIT=0` (disabled) for reload run; default for everything else |
| git rev | `d3ec1fd` (`core: harden admin trust path and bound delivery queues`) |
| start state | working tree had `M core/supervisor.py`, plus two M tests; no impact on Core wire path |

### Methodology rules applied

1. Each sub-bench gets a **warmup phase** (≥100 invocations) before
   timing starts.
2. Each sub-bench reports `n`, `min`, `p50`, `p90/p95/p99`, `p999`,
   `max`, `mean`, `stdev`, throughput.
3. ASCII histograms cap the upper bin at **p99** so the long-tail
   spillover doesn't squash the bulk of the distribution. The last bar
   reports "≥ p99" count.
4. All measurements are wall-clock latency from `time.perf_counter()`
   on the client side (i.e. include the SDK's HTTP POST, Core's full
   routing, the SSE deliver to the echo node, the echo node's response
   POST, and Core's future fulfilment back to the client).
5. **No fork-and-poll**: the Python driver awaits invocations on a
   single asyncio loop, so reported throughput at higher concurrency is
   the throughput Core can sustain, not the throughput of N OS
   processes hammering it.
6. Cold-boot timing kills any prior process on `:8000`, then spawns
   Core, polls `/v0/healthz` until 200, spawns the echo node, polls
   `/v0/healthz` until `nodes_connected == 1`, runs one `dummy_actor`
   one-shot invocation, then tears down. 10 trials.

To re-run later:

```bash
# rev d3ec1fd or later
cd RAVEN_MESH
source scripts/_env.sh
export BENCH_CLIENT_SECRET=$(printf "mesh:bench_client:dev" | shasum -a 256 | cut -d' ' -f1)
export BENCH_ECHO_SECRET=$(printf "mesh:bench_echo:dev"   | shasum -a 256 | cut -d' ' -f1)
export ADMIN_TOKEN=bench-admin-token
ADMIN_TOKEN=$ADMIN_TOKEN AUDIT_LOG=audit_bench.log MESH_ADMIN_RATE_LIMIT=0 \
    python3 -m core.core --manifest manifests/bench.yaml --port 8000 &
python3 -m nodes.dummy.dummy_capability --node-id bench_echo &
python3 scripts/bench/python_bench.py latency    --iters 5000
python3 scripts/bench/python_bench.py throughput --concurrency 16 --duration 8
python3 scripts/bench/python_bench.py payload    --bytes 4096 --iters 1000
python3 scripts/bench/python_bench.py firefoget  --iters 5000
python3 scripts/bench/http_baseline.py --mode serial --iters 5000
python3 scripts/bench/reload_bench.py  --iters 200
bash    scripts/bench/boot_bench.sh    10
```

---

## B1 — Single-flight protocol round-trip latency

`bench_client → bench_echo.ping` request/response, payload `{"i": <int>}`,
warmup 200, n=5000.

```
{
  "n": 5000,
  "min":   0.413 ms,
  "p50":   0.445 ms,
  "p90":   0.469 ms,
  "p95":   0.477 ms,
  "p99":   0.494 ms,
  "p999":  0.558 ms,
  "max":   0.613 ms,
  "mean":  0.447 ms,
  "stdev": 0.017 ms,
  "throughput_rps": 2235.6
}
```

ASCII histogram (range min..p99, last bin = ≥ p99):

```
0.413ms |#                                                           | 10
0.417ms |######                                                      | 59
0.421ms |###################                                         | 178
0.425ms |##########################################                  | 382
0.429ms |##########################################################  | 531
0.433ms |############################################################| 541
0.437ms |#####################################################       | 479
0.441ms |################################################            | 437
0.446ms |##########################################                  | 381
0.450ms |##########################################                  | 382
0.454ms |##########################################                  | 384
0.458ms |###############################                             | 285
0.462ms |################################                            | 297
0.466ms |####################                                        | 186
0.470ms |################                                            | 150
0.474ms |##########                                                  | 99
0.478ms |#########                                                   | 85
0.482ms |#####                                                       | 50
0.486ms |##                                                          | 19
0.490ms |#######                                                     | 65
```

Distribution is unimodal, very tight (stdev ≈ 17 µs). The tail beyond
p99 is only 65 samples ≥ 0.49 ms with max 0.61 ms — no GC pauses or
event-loop hiccups visible in this window.

## B2 — Throughput sweep under concurrency

Same path, payload `{"i":0}`, fixed driver host, `duration=8s` per cell,
warmup 100.

| concurrency | n      | p50 ms | p95 ms | p99 ms | rps     | errors |
|:-----------:|-------:|-------:|-------:|-------:|--------:|-------:|
| 1           | 17 570 |  0.449 |  0.492 |  0.546 | 2 196   | 0      |
| 4           | 33 489 |  0.946 |  1.037 |  1.185 | 4 186   | 0      |
| 16          | 36 989 |  3.437 |  3.619 |  3.878 | 4 624   | 0      |
| 64          | 38 694 | 13.147 | 13.860 | 17.022 | 4 837   | 0      |
| 256         | 38 381 | 20.659 | 21.554 | 25.181 | 4 798   | 0      |

Saturation knee is around **c=4–16**: at c=4 the throughput more than
doubles single-client; from c=16 upward latency rises roughly linearly
with concurrency while throughput plateaus near 4.8 k rps. That's the
ceiling for one Core process on this hardware. Little's law sanity:
4 798 rps × 0.0207 s ≈ 99 in-flight requests at c=256, consistent with
the 256-deep client backlog amortising across the loop.

ASCII histogram at **c=64** (p50 13.1 ms, p99 17.0 ms):

```
1.567ms |                                                            | 1
2.340ms |                                                            | 0
3.113ms |                                                            | 0
3.885ms |                                                            | 1
4.658ms |                                                            | 0
5.431ms |                                                            | 0
6.204ms |                                                            | 0
6.976ms |                                                            | 0
7.749ms |                                                            | 0
8.522ms |                                                            | 22
9.295ms |                                                            | 12
10.067ms |                                                           | 28
10.840ms |                                                           | 29
11.613ms |#                                                          | 415
12.386ms |############################################################| 19331
13.159ms |#####################################################      | 17195
13.931ms |##                                                         | 663
14.704ms |                                                           | 187
15.477ms |                                                           | 216
16.250ms |#                                                          | 594
```

A small tail spike at 16.25 ms+ (594 samples) suggests occasional
queueing behind another in-flight invocation; nothing pathological.

## B3 — HTTP floor (raw `/v0/healthz`)

Same client lib, same loopback. **No protocol work** — just `aiohttp`
GET + `web.json_response`.

| concurrency | mode       | n       | p50 ms | p95 ms | p99 ms | rps     |
|:-----------:|------------|--------:|-------:|-------:|-------:|--------:|
| 1 (serial)  | serial     |   5 000 | 0.089  | 0.121  | 0.141  | 10 549  |
| 1           | concurrent |  62 456 | 0.089  | 0.117  | 0.124  | 10 409  |
| 4           | concurrent | 176 687 | 0.133  | 0.146  | 0.184  | 29 448  |
| 16          | concurrent | 185 119 | 0.513  | 0.570  | 0.598  | 30 853  |
| 64          | concurrent | 210 405 | 1.821  | 1.923  | 2.032  | 35 068  |
| 256         | concurrent | 191 151 | 3.142  | 3.302  | 3.414  | 31 859  |

So a stateless GET on the same Core process on the same hardware peaks
at about **35 k rps**. The protocol round-trip path peaks at about
**4.8 k rps**. The protocol does ~7× the work per request and that is
roughly accounted for by:

1. Two HTTP roundtrips per invocation (POST `/v0/invoke` + POST
   `/v0/respond`).
2. Two SSE writes through Core (deliver + heartbeat scheduling).
3. Four HMAC operations (sign+verify on `/v0/invoke`, sign+verify on
   `/v0/respond`).
4. JSON canonicalisation on every signed envelope.
5. JSON-Schema `validate()` against the surface's schema on every
   request (the permissive `echo.json` is essentially free, so this is
   a *floor*; non-trivial schemas would cost more).
6. ACL edge lookup, audit-log line write, and admin SSE tap fan-out.

That is the *protocol-level* cost. The numbers above are the impl-layer
realisation of that cost. **Item 4 (JSON canonicalisation on every
envelope) is the most attractive target for a future impl** — it
serialises a deepcopy-equivalent dict twice per envelope.

## B4 — Fire-and-forget

Same client, target `bench_echo.ff` (declared as `fire_and_forget`),
warmup 200, n=5000, `wait=False` (HTTP 202 returned as soon as Core
queues the deliver event).

```
{
  "n": 5000,
  "min": 0.201 ms, "p50": 0.247 ms, "p90": 0.508 ms,
  "p95": 0.838 ms, "p99": 1.348 ms, "p999": 4.845 ms,
  "max": 11.565 ms, "mean": 0.334 ms, "stdev": 0.350 ms,
  "throughput_rps": 2995.0
}
```

Histogram (range 0.201..1.348 ms):

```
0.201ms |############################################################| 2900
0.259ms |####################                                        | 989
0.316ms |######                                                      | 321
0.373ms |###                                                         | 177
0.431ms |#                                                           | 89
0.488ms |#                                                           | 65
0.545ms |                                                            | 48
0.603ms |                                                            | 33
0.660ms |                                                            | 44
0.717ms |                                                            | 38
0.775ms |                                                            | 41
0.832ms |                                                            | 41
0.889ms |                                                            | 35
0.947ms |                                                            | 36
1.004ms |                                                            | 22
1.061ms |                                                            | 21
1.118ms |                                                            | 17
1.176ms |                                                            | 11
1.233ms |                                                            | 14
1.290ms |#                                                           | 58
```

Fire-and-forget is **~1.8× faster** at p50 (0.247 vs 0.445 ms) and
**~1.4× higher single-client throughput** (3 000 vs 2 200 rps). Saved
work: the second HTTP roundtrip + response sign/verify + future await.
The tail is wider because the client doesn't synchronise with Core's
queue drain — when the deliver queue backs up, the 202 ACK still
returns fast but the bench's loop body skews.

## B5 — Payload size scaling

Single-flight, c=1, 1000 iters, varying outbound payload bytes (the
echo handler echoes the same data back, so the *response* is comparable
in size).

| payload (bytes) | p50 ms | p95 ms | p99 ms | rps    |
|----------------:|-------:|-------:|-------:|-------:|
| 64              | 0.446  | 0.489  | 0.672  | 2 201  |
| 256             | 0.458  | 0.489  | 0.528  | 2 141  |
| 1 024           | 0.469  | 0.511  | 0.582  | 2 097  |
| 4 096           | 0.534  | 0.560  | 0.620  | 1 862  |
| 16 384          | 0.793  | 0.845  | 0.901  | 1 259  |
| 65 536          | 1.548  | 1.611  | 1.738  |   648  |
| 262 144         | **timeout** (impl-layer bug — see below) | | | |

Below ~4 KB the protocol cost dominates (latency essentially flat).
From 4 KB upward latency tracks payload size sub-linearly until the
SSE-readline cap kicks in.

> ⚠️ **Impl-layer bug discovered.** At 256 KB the receiving echo node
> dies with:
> `Got more than 131072 bytes when reading: b'data: {"id": ...'`
> This is `aiohttp.StreamReader.readline()`'s default
> `max_line_size=131072` on the SSE consumer (`node_sdk._stream_loop`).
> An envelope whose JSON body crosses ~128 KB hits that limit and the
> SSE socket closes 400. The wire-protocol spec doesn't specify
> SSE-as-transport, so this isn't a protocol bug — but it *is* an
> immediate impl bug that we should fix in `node_sdk/__init__.py`
> (pass `read_bufsize=...` or use `readuntil(b"\n\n")` with a larger
> buffer). Tag: **opinionated/impl-layer fix**.

## B6 — Manifest reload (`/v0/admin/reload`)

`MESH_ADMIN_RATE_LIMIT=0` (otherwise capped at 60/min, 20-burst).
Manifest = the bench manifest (2 nodes, 4 surfaces, 4 schemas), warmup
20, n=200.

```
{ "n": 200, "min": 1.085 ms, "p50": 1.110 ms,
  "p95": 1.162 ms, "p99": 1.267 ms, "max": 1.316 ms,
  "mean": 1.118 ms }
```

Reload re-reads the manifest YAML, re-parses & loads each surface's
JSON Schema from disk (4 here), re-walks `relationships`, and rebuilds
`state.nodes_decl` + `state.edges`. Live SSE connections persist; only
the declarative state is swapped. ~1.1 ms scales with `O(nodes ×
surfaces)` and one disk read per schema; for the full demo (5 nodes,
~25 surfaces) you'd expect ~3–5 ms.

## B7 — Cold boot

10 trials. Each trial: kill `:8000`, spawn `python -m core.core`,
poll `/v0/healthz` until 200, spawn `dummy_capability`, poll until
`nodes_connected == 1`, fire one `dummy_actor` round-trip, tear down.

| trial | core healthy (s) | echo connected (s) | first invocation (s) |
|:----:|----------------:|-------------------:|---------------------:|
| 1    | 0.154           | 0.281              | 0.406                |
| 2    | 0.161           | 0.299              | 0.423                |
| 3    | 0.162           | 0.298              | 0.421                |
| 4    | 0.157           | 0.299              | 0.422                |
| 5    | 0.169           | 0.298              | 0.420                |
| 6    | 0.134           | 0.262              | 0.384                |
| 7    | 0.169           | 0.309              | 0.437                |
| 8    | 0.167           | 0.306              | 0.429                |
| 9    | 0.164           | 0.298              | 0.421                |
| 10   | 0.167           | 0.311              | 0.433                |
| **mean** | **0.160**   | **0.296**          | **0.420**            |

~280 ms of the 420 ms is "Python spawned three times" (Core, echo node,
one-shot actor) — each Python interp startup eats ~100 ms on this host
before anything mesh-specific runs. The mesh-specific work (manifest
load + schema parse + register + SSE handshake + invoke + sign + verify
+ deliver + respond + verify) fits in the remaining ~140 ms across all
three processes. A long-running setup that pays Python startup once
would see "first invocation against a warm Core" closer to the **B1
single-flight 0.45 ms** number.

## ASCII trend chart — protocol throughput vs HTTP floor

```
  rps (k)
   35 |                                                  ●●●          /healthz GET
   30 |                                          ●
   25 |
   20 |
   15 |
   10 |  ●
    5 |                          ▲▲▲▲    ▲▲▲▲    ▲▲▲▲   ▲▲▲▲          protocol round-trip
    2 |  ▲
    0 +--+----+----+-----+------+------+-------------------------- concurrency
       1   4    16    64   256
```
(approximate — see tables for exact values)

## Notes for whoever re-runs this

1. **Bench artefacts kept in repo:** `manifests/bench.yaml`,
   `scripts/bench/{python_bench.py,http_baseline.py,reload_bench.py,
   boot_bench.sh}`. They are isolated from prod manifests and can be
   removed with one `rm -r`. Marked **impl-layer / dev-tooling**, do
   not depend on these from product code.
2. **Don't compare against today's headline numbers without
   re-checking the git rev** — the Core invocation hot path was
   recently hardened (`d3ec1fd`); future hardening (e.g. nonce stores,
   replay caches, per-edge token buckets) will move the numbers.
3. **The bench rig deliberately does not measure node-business-logic
   cost.** That is by design — that lives in the opinionated layer
   (kanban, voice, etc.), and per `PROTOCOL_CONSTRAINT.md` should not
   be folded into protocol numbers. To bench a specific node, add a
   manifest specific to that node and reuse `python_bench.py`.
4. **Why no `wrk`/`hey`?** Not installed on this Mac. The aiohttp
   loop in `http_baseline.py` is a perfectly fine *relative*
   substitute for protocol-vs-HTTP comparison on the same loopback.
   For an absolute rps ceiling on `/healthz` install `hey` and re-run
   — expect 60–80 k rps at c=64 since `hey` does not pay for Python on
   the client side.
5. **Audit log impact.** Each routed invocation appends one JSON line
   (~250 bytes) under an `asyncio.Lock`. At 4.8 k rps that's ~1.2 MB/s
   of synchronous file I/O. The Mac mini's SSD swallows this without
   measurable backpressure in this run, but on slower disks the audit
   log will become the bottleneck. **Impl-layer optimisation target**:
   batched flushes or rotate-on-size, not a protocol change.
6. **Action items** (impl-layer, none touch the protocol):
   - Fix `node_sdk` SSE readline cap (B5 bug).
   - Consider canonical-JSON memoisation when the same envelope is
     signed twice in a hot path.
   - Bench again after the supervisor-owned process model lands;
     spawn cost is currently outside the loop.

---

*End of report. ~1500 words excluding tables.*
