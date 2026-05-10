---
title: RAVEN_MESH v1 — Benchmark Design
author: benchmark design worker
date: 2026-05-10
status: DESIGN — feeds into the v1 conformance suite and the next bench run
constraint: PROTOCOL_CONSTRAINT.md — every benchmark tagged
  protocol-layer | opinionated/impl-layer
---

# 0. Layer split

Two costs live in this repo and must not be conflated:

- **Protocol-layer cost** — what the wire contract requires *any*
  conformant impl to do per envelope: HMAC sign+verify, JSON-Schema
  validate, edge-ACL lookup, replay/nonce check, deliver, respond,
  audit-log line. These survive a Python → Elixir/Go/Rust port and
  are what the conformance suite pins SLAs against.
- **Opinionated/impl-layer cost** — what *today's Python prototype*
  pays on top: aiohttp parsing, SSE-as-transport, single asyncio loop,
  `jsonschema`'s pure-Python validator, audit-log file lock. These
  change without the protocol changing.

Every benchmark below is layer-tagged. Protocol-layer SLAs become
part of the v1 conformance contract; impl-layer numbers are
diagnostics for this Python build only.

Fixtures (`manifests/bench.yaml`, dummy capability) are **deliberately
trivial echo nodes** — `return {"echo": payload}` with permissive
`schemas/echo.json`. No business logic, so what we measure is
protocol routing + impl wire path, not node work. A non-trivial node
would leak opinionated cost into the protocol numbers.

# 1. What we benchmark for v1

Six families. Each is described below as: **fixture → methodology →
SLA → what "broken" looks like → tooling**.

The host of record for v1 SLAs is the Mac mini M4 (10 perf cores,
16 GB) used in `benchmark_results_20260510.md`. SLAs scale with
hardware; what is invariant across hardware is the *ratio* between
the protocol round-trip and the raw HTTP floor on the same box.

---

## B1 — Invoke latency, single node

**Layer:** protocol (SLA), impl (absolute numbers).

**Fixture.** `manifests/bench.yaml` — one actor (`bench_client`),
one capability (`bench_echo.ping`), one declared edge, permissive
echo schema, 64-byte payload `{"i": <int>}`. Loopback only. One Core
process; no supervisor. This is the simplest possible request/response
path through the wire contract.

**Methodology.**
1. Start Core, register echo node, wait until `nodes_connected == 1`.
2. Fire 200 invocations as warmup; discard timings.
3. Fire `n=5000` serial invocations, c=1, time each with
   `time.perf_counter()` from before `invoke()` to after the response
   future fulfils on the client side. Wall-clock, client-observed.
4. Record `min, p50, p90, p95, p99, p999, max, mean, stdev` plus a
   histogram capped at p99 so the bulk distribution is readable.
5. Repeat with payloads 64 / 256 / 1024 / 4096 / 16 384 / 65 536 B.

**Target SLA (protocol layer).** *Protocol overhead* on a single-flight
invoke ≤ **5×** the host's raw HTTP floor (a no-op GET on the same
process and client lib). At higher hardware floors the absolute
latency drops; the *ratio* is the conformance pin. On the Mac mini
M4 reference host this lands at p50 ≤ 0.5 ms, p99 ≤ 0.7 ms for ≤1 KB
payloads.

**SLA (impl).** Today's Python build: p50 ≤ 0.5 ms, p99 ≤ 0.6 ms at
64 B; payload-size scaling sub-linear up to the SSE-readline cap (an
impl bug, see B5 in `benchmark_results_20260510.md`).

**What "broken" looks like.**
- Multi-modal latency (two humps in the histogram) → contention or
  GC.
- p99/p50 ratio > 3× → tail amplifying; check audit-log lock,
  asyncio scheduler, JSON canonicalisation cost.
- Latency rising super-linearly with payload size → schema validator
  re-walking blobs, or buffer copies in the SSE writer.

**Tools.** `scripts/bench/python_bench.py latency` (uses the same
`node_sdk.MeshNode` real nodes use, so we measure the actual path).
For the HTTP floor against `/v0/healthz` prefer **`hey`** or **`wrk`**
— both saturate loopback better than a Python aiohttp loop. Neither
is installed on the reference host today; `scripts/bench/http_baseline.py`
(an aiohttp loop) is an acceptable *relative* substitute. v1 should
ship a `make bench-http-floor` target that prefers `hey` and falls
back to the aiohttp loop.

---

## B2 — Invoke latency, multi-hop

**Layer:** protocol.

**Fixture.** Three-node manifest: `bench_client → bench_router →
bench_echo`. `bench_router` is a second copy of the dummy capability
that re-`invoke`s downstream and returns the result. Two declared
edges. The whole flow exercises the protocol end-to-end *twice*: one
sign+verify+validate+ACL+deliver+respond stack, then a second one
nested inside the first.

**Methodology.** Same driver as B1, n=5000, payload 64 B. Compare
the per-hop cost: (multi-hop p50) − (single-hop p50). That delta is
the **per-hop protocol overhead** — what each additional waypoint
costs — and is the more meaningful number for routing-heavy designs
than absolute multi-hop latency.

**Target SLA (protocol).** Per-hop additional cost ≤ 1.6× the
single-hop cost (i.e. two hops cost no more than 2.6× one hop). Linear
scaling. Anything super-linear means the protocol or the impl is
serialising hops behind a shared lock.

**What "broken" looks like.**
- Two-hop is > 3× one-hop → audit-log lock contention or shared
  state in the routing path.
- Tail blows up at hop 2 even though hop 1 is fine → bounded queue
  pressure on the inner deliver.

**Tools.** `python_bench.py latency --route multihop` (new flag).
Same percentile reporter, same histogram. Optionally `locust` with a
custom `User` driving multi-hop chains for distribution-shape
inspection at scale.

---

## B3 — Envelope throughput, sustained

**Layer:** protocol (saturation shape, knee location), impl (absolute
ceiling).

**Fixture.** Same as B1. The driver fires invocations against
`bench_echo.ping` at increasing concurrency.

**Methodology.**
1. Sweep `c ∈ {1, 4, 16, 64, 256}`. For each cell, run `duration=8s`
   (longer if Core hasn't reached steady state — confirm by checking
   that rps is stable across two consecutive 1s windows).
2. Each cell reports `n, p50, p95, p99, rps, errors`.
3. Use **Little's law** as a sanity check: `in_flight ≈ rps × p50`.
   When that number diverges from the configured concurrency, the
   driver is the bottleneck — re-run with a faster client.
4. Note where the **knee** is — the concurrency at which rps stops
   rising. Above the knee, latency scales with concurrency; rps does
   not. The knee location is what we care about, not the absolute rps.

**Target SLA (protocol).** Single-node throughput must scale linearly
with concurrency until the knee, then plateau (not collapse). No
errors, no SSE deliver-queue overflows at any cell, audit-log writes
not the bottleneck (verify with `iostat`).

**SLA (impl).** Mac mini M4 reference: knee at c=4–16, plateau
≥ 4 k routed envelopes/sec at c≥64. (Today's measured ceiling is
~4.8 k rps; see `benchmark_results_20260510.md` B2.) An Elixir/Go/
Rust impl is expected to lift this by at least an order of magnitude
on the same hardware; if the v1 protocol allows for that headroom,
the *protocol* is fine even when *this impl* isn't.

**What "broken" looks like.**
- Throughput *falls* past the knee (collapse) → unbounded queueing,
  no flow control.
- 5xx errors above c=64 → SSE deliver-queue overflow without the
  503 + `denied_queue_full` audit (HR-4 violation).
- Audit log write latency > p99 invocation latency → audit lock is
  the bottleneck, not the wire.

**Tools.** `scripts/bench/python_bench.py throughput --concurrency N
--duration 8`. **`wrk`** (`-t8 -c64 -d8s`) against a wrapper endpoint
that triggers an admin-synthesised invocation gives a non-Python
client comparison from outside the process model. **`locust`** is
right for opinionated-layer "real demo flow" benches with synthetic
users + think-time, but for the protocol layer its greenlet-per-user
model adds noise we don't want.

---

## B4 — SSE fan-out scale (10 / 100 / 1000 subscribers)

**Layer:** protocol — but only the *parts of the spec that touch SSE*
(deliver-queue bound, Last-Event-ID resume per HR-5, drop-on-overflow
per HR-4). The transport itself is impl.

**Fixture.** One `bench_emitter` capability declares N subscriber
edges (or alternatively, one fan-out broadcast surface that all N
subscribers consume). N attached subscribers (cheap echo readers that
discard payloads). The emitter publishes envelopes at fixed rate
(start at 100/s).

**Methodology.**
1. **Static:** Spin up N ∈ {10, 100, 1000} subscribers; emitter
   publishes at 100 envelopes/s for 60 s. Measure: per-subscriber
   delivery p50/p95/p99 (timestamp envelope on emitter, mark on
   subscriber receipt — both same loopback so clock skew is nil),
   subscribers that fell behind (queue-full count from Core), CPU on
   the Core process.
2. **Backpressure:** With N=1000 subscribers, ramp emit rate from
   100 → 5000/s in steps. Confirm that slow subscribers get dropped
   per HR-4 with `denied_queue_full` audit entries, *and* that fast
   subscribers continue to receive without latency degradation. The
   protocol claim is "drop the slow consumer, not the system."
3. **Resume:** With N=100, kill 10 subscriber TCP connections
   mid-stream, reconnect with `Last-Event-ID` set to the last id
   each saw. Verify zero gaps (HR-5).

**Target SLA (protocol).**
- N=10: per-subscriber p99 deliver ≤ p99 single-flight invoke + 0.5 ms.
- N=100: same +1 ms.
- N=1000: same +5 ms; *no* effect on B1 single-flight latency on a
  separate edge (i.e. the protocol's fan-out path doesn't poison the
  request/response path).
- Drop-on-overflow always emits a `denied_queue_full` audit entry.
- `Last-Event-ID` resume after disconnect: zero-gap delivery for
  events still in the per-node ring buffer; explicit `resume_gap`
  audit when the requested id has rolled out.

**What "broken" looks like.**
- A slow subscriber slows everyone → producer-blocking (HR-4
  violation, the bound is meant to drop, not block).
- Latency on B1 path rises while N=1000 fan-out is active → shared
  event loop saturation; not a protocol bug per se but the impl
  needs to spread SSE write work across cores.
- Reconnect with `Last-Event-ID` returns nothing → HR-5 not wired.

**Tools.** A new `scripts/bench/sse_fanout.py` aiohttp subscriber
driver (each subscriber is a coroutine, not a process, to keep
host-side noise low). For scale beyond one host, **`locust`** with a
custom user that opens an SSE stream is the standard answer; not
needed for v1's 1000-subscriber loopback target. **`asyncio-bench`**
is a useful primitive on the subscriber side but not required.

---

## B5 — Supervisor restart latency

**Layer:** protocol — restart-strategy semantics (HR-style:
`permanent | transient | temporary`, declared in the manifest) are
part of the wire/manifest contract. Absolute restart wall-time is
impl.

**Fixture.** Bench manifest extended to mark `bench_echo` as
`restart_policy: permanent` with a small backoff. The supervisor owns
the process. The driver invokes `bench_echo.ping` continuously at
low rate (10/s).

**Methodology.**
1. Steady state: confirm round-trips succeed, supervisor reports
   `running`.
2. Kill the echo node's PID with `SIGKILL` from outside.
3. Time, in milliseconds, from kill until:
   - **t1** Supervisor observes `Process.wait()` return.
   - **t2** Supervisor finishes spawn + the new process registers.
   - **t3** First successful invocation post-restart (the driver
     observes a successful response after a failure window).
4. Repeat 30 times to get a distribution. Report all three (`p50`,
   `p95`, `p99`) for each of t1, t2, t3.
5. Separately: invoke the node *during* the restart gap and confirm
   the driver sees either a fast 503 (delivery refused, node not
   connected) or a queued deliver that resolves once the new process
   registers — whichever the spec mandates. v1 should mandate the
   former; queueing across restart muddies failure semantics.

**Target SLA (protocol).** The contract guarantees: detection is
immediate (next event-loop tick after `wait()` returns), restart is
attempted with declared backoff, in-flight invocations either fail
fast or succeed — never silently lost. Numerically: t3 ≤ 1.5 s on
the reference host (Python startup is ~280 ms × the spawn overhead).

**What "broken" looks like.**
- t1 > 100 ms → supervisor isn't actually `await`ing the child.
- t3 wildly variable run-to-run → restart backoff jitter dominates;
  acceptable if bounded and declared.
- Some invocations during the gap return 200 with stale data → the
  protocol is silently routing to a corpse, which is a wire-spec
  failure, not just an impl bug.

**Tools.** `scripts/bench/supervisor_restart.py` (new): manages the
chaos kill via `os.kill`, polls `/v0/admin/processes` for state
transitions, drives invocations on a separate task. Uses
`time.perf_counter()` for the timestamps. No external tools needed —
this is a control-plane benchmark, not a load test.

---

## B6 — Signature verification throughput

**Layer:** protocol — HMAC verification *is* part of the wire
contract; its cost characteristics matter to anyone implementing the
protocol. Impl: which `hmac` impl, which JSON canonicaliser.

**Fixture.** Standalone microbench, **no Core**. A tight loop that:
1. Builds an envelope dict (typical shape from
   `schemas/manifest.json` semantics: `id, from, to, surface, body,
   nonce, timestamp`).
2. Canonicalises it (the same canonical-JSON function Core uses).
3. HMAC-SHA-256-signs it.
4. Verifies it.
5. Repeats N=100 000 times.

Measure ops/sec for **sign-only**, **verify-only**, and the
**sign+verify pair** (which is what the wire path actually pays per
hop). Report under varying body sizes (0, 64 B, 1 KB, 64 KB).

**Target SLA (protocol).** Sign+verify must be cheap enough that on
the host's hardware, four sign/verify ops per envelope (HR-1's
nonce/replay path doubles that) cost ≤ 25% of B1 single-flight
latency. On the Mac mini M4: ≥ 250 k sign+verify pair-ops/sec for
empty body, ≥ 100 k for 1 KB body. If we fall below this, JSON
canonicalisation — not HMAC itself — is almost always the cause; the
fix lives in the impl.

**What "broken" looks like.**
- Sign+verify > 1× B1 single-flight latency → the canonicalisation
  function is allocating; consider a streaming variant.
- Throughput collapses at 64 KB body → canonicaliser is O(n²) on
  body size (a real bug seen in early prototypes).
- Sign vs verify cost asymmetric > 2× → the verifier is recomputing
  state it should reuse.

**Tools.** `scripts/bench/sign_verify.py` (microbench, no Core).
Use **`pyperf`** for the Python impl — its warm-ups, GC controls, and
stable median reporting are what microbenches want. For non-Python
impls: rust `criterion`, go `testing.B`, elixir `benchee`. The
conformance suite should keep one canonical "1000 envelopes, expected
output" fixture so all impls can verify byte-identical canonicaliser
output — a correctness anchor for the perf numbers.

---

## B7 — Manifest reload latency

**Layer:** protocol — `/v0/admin/reload` is part of the wire contract
(HR-6, manifest-validator strict mode). The cost shape (linear in
nodes × surfaces × schemas, with one disk read per schema) should be
documented.

**Fixture.** Bench manifest (2 nodes, 4 surfaces, 4 schemas) and a
larger reference manifest (`manifests/full_demo.yaml`, ~5 nodes,
~25 surfaces). `MESH_ADMIN_RATE_LIMIT=0` so the rate-limiter doesn't
shape the run.

**Methodology.**
1. Warmup 20 calls.
2. Fire 200 `POST /v0/admin/reload` calls back-to-back, time each.
3. Report `min, p50, p95, p99, max`.
4. Repeat with the full demo manifest.
5. Optional perturbation: while a reload runs, fire B1 invocations
   on a separate task and verify they don't fail (live SSE
   connections must persist across reload — only declarative state
   swaps, per the design).

**Target SLA (protocol).** Reload is `O(nodes × surfaces)` plus one
disk read per distinct schema. p99 ≤ 5 ms for a small manifest, ≤
50 ms for any realistically-sized manifest (≤ 100 nodes). Live
invocations during reload do not fail. No envelopes lost.

**What "broken" looks like.**
- Reload time grows super-linearly with manifest size → the
  validator is re-walking the graph quadratically.
- An in-flight invocation fails with "unknown edge" during reload →
  the swap isn't atomic.
- Reload disconnects SSE consumers → subscriber state was tied to
  declarative state, which it shouldn't be.

**Tools.** `scripts/bench/reload_bench.py` already exists.
`hey -n 200 -c 1` against `/v0/admin/reload` is a one-liner sanity
check that confirms server-side numbers.

---

# 2. Tooling matrix

| Need | First choice | Why |
|---|---|---|
| HTTP floor (loopback) | **`hey`** | Simple, fast, no Python tax on client |
| HTTP floor (richer histograms) | **`wrk`** + Lua script | Custom request bodies, %ile output |
| User-flow load (opinionated layer) | **`locust`** | Python `User` classes, distributable |
| In-process Python microbench | **`pyperf`** | Stable medians, GC handling |
| In-process async driver | bespoke `asyncio` script + `node_sdk` | Drives the *real* SDK code path |
| SSE subscribers at scale | bespoke aiohttp coroutines; `locust` if distributing | aiohttp is the same lib the SDK uses |

`asyncio-bench` is a useful primitive on the driver side but not
required; in-tree `python_bench.py` handles warmup + percentile +
histogram in <200 lines and stays aligned with the SDK's call shape.

# 3. Deliberately out of scope

- **Distributed / multi-host throughput.** v1 protocol doesn't mandate
  federation (v1.1, see `experiments/multi_host`). When it does, B3
  needs a network-loss / latency variant.
- **Real-node business-logic numbers.** Kanban, voice, nexus_agent
  are opinionated-layer; their perf is each node's problem. Protocol
  numbers must not absorb their cost.
- **Cold-start.** Bench results from 2026-05-10 show v1 cold boot is
  dominated by Python interpreter startup × N processes (~100 ms
  each) — an impl issue future BEAM/Rust impls will trivialise.

# 4. Output expectations

Per subbench:
- JSON sidecar with raw samples (alongside `audit_bench.log`).
- Markdown summary in `notes/benchmark_results_YYYYMMDD.md`:
  percentile table, ASCII histogram capped at p99, layer tag at top.
- Git rev, hardware, Python/aiohttp versions pinned so runs are
  comparable.

The conformance suite, when it lands, turns the protocol-layer SLAs
above into pass/fail gates — *normalised against the host's HTTP-floor
measurement*, not absolute milliseconds. That's the only way an SLA
written today survives a hardware refresh or an impl rewrite.

---

*All recommendations layer-tagged per `PROTOCOL_CONSTRAINT.md`. No
iMessage path used.*
