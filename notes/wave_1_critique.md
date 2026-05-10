# Wave 1 Critique — brutal review

**Author:** wave-2 critic worker
**Date:** 2026-05-10
**Scope:** every wave-1 artifact in `notes/` plus `notes/security_patches/*` and
`notes/capability_graph/*`. Verdict: a *lot* of motion, a fair amount of
hand-waving, and one specific failure mode that crops up in three different
docs — **product opinion leaking into the protocol** under the cover of
"protocol-layer" tagging.

The structure below is harsh on purpose. Praise where deserved, but the
brief asked for hand-wave hunting and that's the bulk of what's here.

---

## 1. Hand-waves

### 1.1 `synthesis_20260510.md` — the dashboard-as-node bombshell is misframed

§6 ("One bold proposal") is the most-cited document in wave 1, and the
framing is wrong. The synthesis worker writes: *"the dashboard registers
as a node — `dashboard_node` ... It gets its envelopes signed by an HMAC
secret like every other node"* (§6, ¶3). Fine. Then the proposal
silently expands the protocol with `core.audit_stream`, `core.set_manifest`,
`core.lifecycle.spawn` / `.stop` / `.restart` / `.reconcile`, and
`core.processes` self-surfaces (§6, ¶3–4). That is **six new protocol
surfaces driven by a UI feature list**. The doc tags the result "one
protocol, one auth model" without ever asking whether a non-UI consumer
of the protocol wants those surfaces at all.

The `dashboard_node_v2.md` worker re-frames this honestly in their own §1
Reframe: *"Re-read against `PROTOCOL_CONSTRAINT.md`, that framing leaks
opinion: it makes 'the dashboard' sound load-bearing for the protocol."*
But even v2 doesn't undo the leak — see §3 of this critique.

### 1.2 `architecture_compare.md` — Elixir LOC parity is misleading

The headline comparison is "Elixir 730 LOC vs Python 1,164 LOC at parity"
(§1, line 17). The footnote (§3, ¶3) admits *"the honest costs are JSON
Schema (commit to a maintained fork or write a thin in-house validator
that targets the slice we use; either belongs in the protocol layer and
is bounded work)."* "Bounded work" is doing all the work in that
sentence. JSON Schema Draft 7 is several hundred test cases in the
official suite; `ex_json_schema` is Draft 4 only and stale (§1, line 27).
A defensible LOC comparison adds *whatever it takes to reach parity on
Draft 7+*. None of the wave-1 docs estimates that — and `manifest_validation_design.md`
silently assumes JSON Schema parsing is free, which is true only in
Python.

The `architecture_compare.md` recommendation paragraph (§3, ¶6, lines
65–67) compounds this. It claims *"protocol LOC parity with Python plus
the hardest 337 lines of the prototype is not a protocol win"* about
Rust, then awards Elixir the v1 path despite Elixir's JSON Schema gap
being structurally worse (Rust at least has `jsonschema` Draft 7+).

### 1.3 `synthesis_20260510.md` §3 — the "two concurrency models" finding is half a finding

§3, point 4 (lines 52–53) flags *"`replace_active=True` on voice_actor
silently kills any in-flight Realtime session if a new `start_session`
arrives ... the same pattern is *not* used in nexus_agent."* True. The
synthesis worker proposes: *"Pick one and document the contract — 'actors
serialize' or 'actors replace' — in the SDK or PROTOCOL.md."* That's the
hand-wave. **Both choices are opinion**. A protocol shouldn't pick one
concurrency policy for actors; it should provide the primitives so each
actor declares its own. The right framing is "the protocol exposes a
declared `concurrency: serialize | replace` field per actor surface, the
SDK enforces it." The synthesis worker missed this and pushed for a
single global rule.

### 1.4 `migration_path.md` — circular dependency on opinionated work

§6 ("Six-week milestone breakdown") opens with: *"Implicit prerequisite
(not part of the 6 weeks): the synthesis-§6 *dashboard-as-node* refactor
lands in Python first ... If that's not on the table, add a week 0
between weeks 1 and 2 of the plan below for admin-endpoint porting."*
(lines 97–98). So the **protocol migration plan is gated on an
opinionated product refactor.** That's the leak: a protocol-layer
decision (what implementation language) is now blocked on a specific
product's UI refactor. The fork test fails — a contributor whose product
doesn't have a dashboard cannot start the migration without first
inventing one.

### 1.5 `operational_playbook.md` — quiesce-via-`node_status.visible` is a phantom contract

§3 step 1 (lines 154–157): *"The protocol does not yet have a 'drain'
primitive. Use whatever the opinionated layer offers — for nodes that
voluntarily report `node_status` ... set `visible: false` and add a
`details: {drain: true}` flag. Upstream nodes that respect the
convention will stop sending new invocations."* This entire paragraph
prescribes a *convention that does not exist in any node we have*.
There's no mention of `details.drain` in `core/`, `node_sdk/`, or any
`nodes/*` file at the time of writing. The playbook is documenting a
fictional handshake as if it were operational guidance. If an operator
follows the runbook today, they get nothing — `visible: false` does not
suppress upstream traffic anywhere in the codebase.

### 1.6 `v1_prd_draft.md` HR-12 — the four-strategy enum is two opinions in a trench coat

§2 HR-12 (lines 103–108) says: *"supervisor exposes four restart
strategies: `permanent | transient | temporary | on_demand`. The
`on_demand` strategy spawns on first envelope, idle-reaps after
`idle_shutdown_s`."* The PRD then tags this `[PROTOCOL]`. It isn't.
`on_demand` was designed for a specific use case — LLM agents that are
expensive to keep warm. The PRD §6 ("Process model") gives this away:
*"a different deployment might run a stateless mesh of pure tools where
everything is `on_demand`."* That's exactly the point: `on_demand` is
opinion in a place where the protocol shouldn't pick. A deployment that
hates idle-reap-on-timer because it spikes cold-start latency at p99
should not be forced to acknowledge the strategy exists. The protocol-
layer commitment should be "the supervisor exposes a strategy enum the
deployment can extend"; the four named strategies belong in the
opinionated layer.

---

## 2. Security gaps in proposed patches

### 2.1 `security_patches/03_hmac_replay_protection.patch` — covers register and invoke, **not respond**

The patch (lines 72–98) inserts `_ts_fresh` + `state.remember_id` checks
into `handle_register` and `_route_invocation` only. `handle_respond`
(audit V-03 explicitly calls this out at lines 117–122 of
`security_audit_20260510.md`: *"There is also no `correlation_id`
uniqueness check on responses"*) is untouched. So a captured response
envelope can be replayed at any time the corresponding pending entry
exists; the audit said "low likelihood given uuid4 ids" but the patch
should have closed the foot-gun anyway. **A patch tagged `replay
protection` that omits the response path is half a patch.**

### 2.2 Patch 03 — id format is not enforced

`state.remember_id` rejects duplicates but does not validate that `id`
is a UUIDv4 or any other unforgeable shape. An attacker minting colliding
ids cannot be detected by uniqueness alone — they'd need to be detected
by *format*. Patch is silent on this. Combined with the `signature_pre_verified=True`
path in `handle_admin_invoke` (audit V-04), a holder of the admin token
can synthesise envelopes with any id they like, bypass the LRU because
their ids are fresh, and replay the *contents* through admin-invoke at
will. The patch leaves this open.

### 2.3 Patch 03 — clock skew check has no NTP requirement, no graceful degradation

`CLOCK_SKEW_SECONDS = 60` (line 16) is a hard reject. If the operator's
clock drifts >60s — not uncommon on a sleepy laptop — the entire mesh
stops working with `stale_or_missing_timestamp` errors and no rollback.
The `security_postmortem.md` §9 open question 2 acknowledges this
("Some nodes (cron, batch ingestion) may legitimately produce envelopes
minutes apart from clock drift") and proposes a configurable bound, but
the patch ships a hard-coded 60s anyway. There is no telemetry to detect
mass-rejection. A real fix needs (a) configurable bound, (b) audit
metric on rejected-by-skew rate, (c) graceful behaviour when the
operator's clock is unreliable.

### 2.4 Patch 08 — `_derive` master in `~/.config/raven_mesh/secret_master` does not address NFS / synced directories

`security_postmortem.md` §7 Fix 2 (lines 263–278) generates the master
in the user homedir. The threat model in `security_audit_20260510.md` §1
doesn't address: cloud-synced homedir (Dropbox, iCloud, OneDrive), NFS,
or `rsync`-replicated home. Each of these silently exfiltrates the
master to a second host, which the patch does not warn about. The patch
also doesn't address what happens when two RAVEN_MESH installs share a
homedir (e.g., a developer running prod and dev on the same host) — they
collide on the same master, which means dev secrets equal prod secrets.

### 2.5 Patch 08 — removing `os.environ[var] = val` breaks supervisor children

The audit says: *"In `core.py:_resolve_secret`, raise on `env:VAR` when
the variable is missing, instead of fabricating a fallback."* Sound. But
the supervisor's children read `*_SECRET` from `os.environ` (see
`scripts/_env.sh` and `scripts/run_*.sh`). If Core no longer writes the
fallback into `os.environ`, every child started by the supervisor under
the missing-env path now fails to register. The patch is silent on the
migration path for existing deployments that relied on the autogen
fallback to bootstrap.

### 2.6 Patch 04 — opinion in the protocol, in a security patch

`security_patches/04_admin_invoke_provenance.patch` proposes (per
`security_audit_20260510.md` §V-04 patch direction lines 162–169): *"Refuse
to synthesize from `approval_node` unless an explicit
`?force_approval=1` query is supplied."* **`approval_node` is a node
from this product's specific manifest.** The protocol must not know
what `approval_node` is. The fix belongs in the opinionated layer
(approval_node refuses to be impersonated) or in a generic mechanism
(any node tagged `protect_against_admin_synthesis: true` in the
manifest gets the guard). Hard-coding `approval_node` is a textbook
constraint violation.

### 2.7 Patch 06 — bound is asserted, eviction is not

The hardening doc claims `state.pending` is rolled back on QueueFull
(`security_hardening.md` §3 lines 91–93). The audit's recommendation
(`security_audit_20260510.md` §V-06 lines 215–218) goes further:
*"evict the slow node: on repeated QueueFull, `_close` the stream and
force re-register."* The shipped patch (06) implements neither
eviction nor pending rollback in code; the test
`test_node_queue_is_bounded` only verifies the cap exists at the
asyncio.Queue layer (`security_hardening.md` lines 100–106). **Asserting
the queue has `maxsize=1024` is not the same as asserting the routing
path correctly handles QueueFull**, and the worker explicitly punted
the integration test as "slow and flaky". That's exactly the test that
matters.

### 2.8 V-12 (Last-Event-ID) is moved to wave-1 SSE consolidation but the design is broken

`sse_consolidation.md` line 56 introduces `Last-Event-ID` resume. The
implementation uses `event_id=evt["at"]` (an ISO timestamp; line 65) for
nexus_agent and `event_id=snap["updated_at"]` for kanban. **ISO
timestamps are not monotonically unique under concurrent writes.** Two
events emitted in the same millisecond collide, and the resume logic
(line 50: *"the server skips replay items up to and including the id
sent by the client in `Last-Event-ID`"*) will silently skip events the
client never saw. The doc claims wire-format equivalence (§"Wire-format
diff") but the resume semantics are silently broken on any node with
sub-millisecond event rates.

The doc also notes (§"Subtle behaviours preserved") *"`broadcast()` uses
`put_nowait` and silently drops for `QueueFull`. Slow consumers don't
backpressure producers."* Combined with `Last-Event-ID` resume, the
client has no way to detect that a drop happened — there's no monotonic
sequence number. So the entire resume guarantee is "you'll get events
that weren't dropped, in order, since your last id, except when ids
collide." That's not durability, that's storytelling.

### 2.9 SSE consolidation deliberately punts on `core/core.py`

`sse_consolidation.md` §"Audit" (line 32) excludes `core/core.py` *"its
node stream is the wire-protocol SSE boundary itself, not an inspector
attachment."* This is the load-bearing SSE loop in the entire mesh.
Skipping it means the V-06 queue-bound fix (in core/core.py) and the
SSEHub drop-on-full logic (in node_sdk/sse.py) live as two separate
implementations of the same idea — exactly the duplication the
consolidation was supposed to eliminate. The `Last-Event-ID` resume
also doesn't cover the protocol's own `/v0/stream` endpoint (V-12 in
the audit), so the most critical SSE consumer in the mesh — every
registered node — gains nothing from the consolidation.

### 2.10 Patch 11 — `entrypoint.sh` defensive check misses tmpfs

`security_audit_20260510.md` §V-11 patch direction (lines 372–380)
recommends *"Strip the OAuth cache out of `/agent/ledger/.claude`
between runs (entrypoint deletes `auth.json`/`credentials.json` after
each run, or use a tmpfs mount for `.claude/auth/`)."* Patch 11 ships
the deletion path but not the tmpfs path. macOS docker volumes survive
container teardown; deletion-on-exit relies on entrypoint reaching the
cleanup line, which a `docker stop` interrupts. Tmpfs is the actually
secure option and the patch ducks it.

---

## 3. Benchmarks lack rigor

### 3.1 `benchmark_results_20260510.md` — bimodal latency distribution called "no hiccups"

§B2 (lines 230–253) shows the histogram at c=64 with a modal cluster at
12.4ms (19,331 samples) and a small cluster at 8.5ms (~22 samples), and
the tail at 16.25ms (594 samples). The worker writes (line 207–208):
*"Distribution is unimodal, very tight (stdev ≈ 17 µs). The tail beyond
p99 is only 65 samples ≥ 0.49 ms ... no GC pauses or event-loop hiccups
visible in this window."* That's about **B1 (single-flight)** — the
unimodal claim. But at **B2 c=64** the actual histogram is *visibly
bimodal* (cluster at 8.5ms and at 12.4ms) and the worker characterises
the tail as *"a small tail spike at 16.25 ms+ ... nothing pathological"*
without any analysis of *why* there are two modes. Two modes in a
loopback bench under fixed concurrency means the event loop is
oscillating between two states. That is the interesting result. The
worker missed it.

### 3.2 `MESH_ADMIN_RATE_LIMIT=0` for the reload bench

§B6 (line 374) disables the rate limiter to run the reload bench
*"otherwise capped at 60/min, 20-burst."* So the headline reload
latency (p50 1.110 ms, line 380) is the latency under a config that
nobody runs in production. The honest number includes the rate-limit
middleware overhead. The worker should have run it both ways and
reported both.

### 3.3 No `wrk`/`hey` is a methodology choice the worker hand-waved

§"Methodology" line 92–96: *"wrk and hey are not installed on this host,
so the HTTP-floor substitute is an aiohttp client in a tight async
loop."* Both `wrk` and `hey` are 5MB Go binaries — installing them is
two minutes. The worker punted on absolute throughput numbers under
the cover of "relative comparison is enough." The B3 ceiling claim
(35k rps, line 269) is the **client-side aiohttp loop's ceiling**, not
the server's, and the doc never separates the two. The reader is left
guessing how much of the 7× protocol-vs-HTTP cost is server-side and
how much is the bench harness.

### 3.4 N=1 cold boot, 10 trials

§B7 (lines 397–410) reports 10 trials of cold boot with mean 420 ms.
Ten trials is enough for a mean, not enough for tail. The cold-boot
distribution is precisely the one with macOS-specific tail risk
(Spotlight indexing, GateKeeper notarisation cache, dyld closure
build). Ten samples can't see those. n=100 is the floor.

### 3.5 No CPU profile to back the optimisation target claim

§B3 lines 290–294: *"Item 4 (JSON canonicalisation on every envelope)
is the most attractive target for a future impl — it serialises a
deepcopy-equivalent dict twice per envelope."* No flame graph, no
`py-spy` output, no measurement. The claim is plausible but unproved.
Worse, the claim drives the architecture-comparison conclusion:
*"the byte-identical canonical-JSON property is preserved across all
four prototypes"* — but if canonical JSON is the bottleneck, then
the *latency* property of the prototypes is what should be benched,
and none of them were.

### 3.6 No SSE backpressure or slow-consumer benchmark in any prototype

`architecture_compare.md` matrix line 24 lists native message-passing
primitives across the four prototypes, but the doc never bench-tests
**what happens when the consumer is slow**. The whole V-06 queue-bound
fix is motivated by slow-consumer DoS, and no prototype was measured
under that workload. The Elixir BEAM mailbox claim (§"What the eventual
Elixir rewrite handles for free", `security_audit_20260510.md` lines
547–566) is taken on faith.

### 3.7 Audit log impact dismissed without measurement

§"Notes for whoever re-runs this" item 5 (lines 459–464): *"At 4.8 k
rps that's ~1.2 MB/s of synchronous file I/O. The Mac mini's SSD
swallows this without measurable backpressure in this run, but on
slower disks the audit log will become the bottleneck."* The bench
didn't actually measure what happens with audit-log on a slower
device. The `state.audit_lock` is held during the write — so on any
disk slower than the M4's NVMe, the audit lock becomes a global
serialisation point for the entire mesh. The worker noted this and
moved on.

### 3.8 Supervisor was off

§"Host" line 109: *"Core process | single asyncio loop, no supervisor
(`--supervisor` off)."* The entire process-model story (HR-12, the
on_demand strategy, the supervisor reconcile contract) is unbenched.
The reader doesn't know whether the supervisor adds 1ms or 100ms of
overhead per envelope, whether `ensure_running` is on the hot path,
or whether the spawn-on-first-envelope semantics work at p99 under
load. **All of HR-12 is policy with zero performance evidence.**

---

## 4. Prototype glossing — Rust / Go / Elixir

### 4.1 Go's "180 LOC `:one_for_one` reimpl" was not stress-tested

`architecture_compare.md` §"Go 1.22+" (lines 53–56): *"Goroutines + a
single channel of state ops give you something close to a GenServer for
~40 lines, the stripped binary deploys with `scp`, cold start is in the
noise (< 10 ms), and the race detector caught a real supervisor bug on
first run."* "The race detector caught a real bug" is good
methodology. But the doc never describes what *load* the supervisor
was tested under. Supervision bugs surface under crash storms, not
single-restart cases. No "kill 100 children in a tight loop and verify
the supervisor recovers" test is documented.

### 4.2 Rust supervisor's "337 hardest lines" — what hardness specifically?

§"Rust / tokio + axum" (lines 47–50): *"the supervisor is the hardest
337 lines in the prototype — every restart-policy decision is a
hand-rolled state machine. Lifetimes around the SSE subscriber set,
Send-across-await on the supervisor's recursive respawn, and the
choice between `std::sync::Mutex` and `tokio::sync::Mutex` are all real
friction the Python or Elixir versions never see."* Real friction is
real, but the qualitative description doesn't translate to a number.
No mean-time-to-recovery, no crash-recovery latency, no measurement of
how the Send-across-await constraint manifests at runtime (e.g., does
it serialise all restarts? Does it permit concurrent restarts of
independent children?). The reader gets vibes, not data.

### 4.3 Elixir's BEAM cold-start floor "seconds"

The matrix line 22: *"Cold start → 200 healthz | seconds (BEAM cold
start)"*. "Seconds" is not a number. Rust gets 23 ms, Go gets <10 ms,
Python gets 150 ms. Elixir gets a vibe. Given that the recommendation
hinges in part on cold-start cost (synthesis worker §"Five litmus
tests" item 4 — *"Operational requirement to ship the protocol as a
single artifact with no runtime on the host"*), an actual number for
BEAM cold-start matters. "Seconds" hides whether that's 2s or 8s, which
matters for sub-process-per-request workloads.

### 4.4 No canonical JSON cross-language golden vector test

`architecture_compare.md` §5 (lines 87–89): *"every prototype above
proved byte-compatibility with Python's canonical JSON. The
cross-language conformance test (`tests/test_protocol.py` driven
against any new core) is the thing that keeps this honest, and per the
Elixir analysis is the cheapest 30-minute investment with the largest
option value."* Then no one shipped the test. `migration_path.md` §2
makes it a week-1 task. `v1_prd_draft.md` A3 makes it an acceptance
criterion. **It's the load-bearing artifact across three docs and
nobody wrote it.** Until it exists, the byte-equivalence claim is
trust-me.

---

## 5. Product opinion leaking into the protocol — the critical lens

This is the lens the brief asked for first. The wave-1 outputs are
disciplined about *tagging* layers — every doc has `[PROTOCOL]` /
`[OPINIONATED]` annotations — but tagging is not the same as obeying
the constraint. Several "protocol-layer" recommendations are dashboard-
or agent-shaped opinions in disguise.

### 5.1 `dashboard_node_v2.md` §2 — six dashboard-driven self-surfaces

Lines 28–37: the proposed `core.state`, `core.audit_stream`,
`core.set_manifest`, `core.reload_manifest`, `core.invoke_as`,
`core.lifecycle.{spawn,stop,restart,reconcile}`, `core.processes`
self-surfaces. The doc tags all of these `PROTOCOL-LAYER`. They are
not. They are *the dashboard's outgoing-edge wishlist transcribed into
protocol surfaces*. The substitution test ("delete dashboard, mesh
still works") technically passes — the surfaces just sit there
unused — but the *spec growth* the BEAM rewrite has to absorb is
permanently dashboard-shaped. A non-UI consumer who never wants to
spawn a process via mesh-invoke is now obliged to implement the spawn
surface, or to ship a non-conformant Core.

The honest fix is to ship only `core.state` (read-only introspection)
and `core.set_manifest` (configuration is universal) at the protocol
layer, and put the rest behind the existing `/v0/admin/*` routes which
are explicitly *opinionated implementation* not protocol.

### 5.2 `v1_prd_draft.md` HR-9 — `_capabilities` with implicit (*, *._capabilities) edge

§4 lines 91–98 + §2 HR-9 lines 91–93: every node *automatically*
exposes `_capabilities` and the system grants an implicit edge that
**every node may inspect every other node's authority**. This is a
default that is opinionated. In a multi-tenant mesh, in a privacy-
sensitive mesh, or in a mesh where edges themselves are sensitive
(e.g., they reveal a customer's deployment topology), this default is
an info leak. The protocol should expose the surface but require the
manifest to grant the edges explicitly. The "implicit edge" wording is
the leak.

### 5.3 `v1_prd_draft.md` HR-10 caveats — kanban-shaped

§4 lines 153–168 / §2 HR-10 lines 95–98: caveats merge JSON-Schema
fragments into the surface schema at routing time. The motivating
example throughout `capability_graph/MODEL.md` is *"may only create on
board 'work'"* (§5.2 line 165). That's a kanban-shaped use case driving
a protocol primitive. Other capability systems chose differently —
Meadowcap picks subspace+path+time only; OCapN picks three-party
handoffs; seL4 picks badges and a derivation tree. Picking JSON-Schema
caveats as **the** caveat language is one specific choice motivated by
*this product*'s schema-everywhere ergonomic. A federation node that
needs path-prefix bounds (Meadowcap-style) gets nothing from this
caveat shape. The PRD frames the choice as obvious because *this*
product already validates schemas; that's the leak.

### 5.4 `v1_prd_draft.md` HR-12 `on_demand` — agent-shaped

Already covered in §1.6 above. Re-stated here because it's the same
pattern: a strategy that is universal in name (`on_demand`) but
agent-cost-curve-shaped in design (idle reap on timer is a response
to LLM warm cost, not a generic supervisor primitive).

### 5.5 `migration_path.md` — the entire migration is gated on the dashboard refactor

Already covered in §1.4. Re-stated under this lens: a protocol-layer
migration plan that requires a specific opinionated product to refactor
first is a leak by definition. The fork test fails: a contributor with
no dashboard cannot start.

### 5.6 `manifest_validation_design.md` §"Open questions" — `core` reservation

Line 149: *"I included only `core` (your future self-surface
namespace, per synthesis §6 proposal)."* The validator now reserves
`core` as a node-id because of an **unmerged proposal** (synthesis §6 →
dashboard_node_v2). Even if that proposal lands, the validator is
pre-committing to its specific shape. A different protocol decision
(e.g., self-surfaces live under `_core` or `__protocol__`) means the
validator emits the wrong error message. Reserved namespaces are the
right idea; pre-committing to the specific name from one wave-1 worker's
proposal is the leak.

### 5.7 `operational_playbook.md` — `details.drain` convention treated as protocol

Already covered in §1.5. Re-stated under this lens: the playbook is
prescribing a node convention as if it were universal. Operators who
follow the runbook will conclude the protocol has a drain story. It
doesn't. The leak is in tone — the runbook reads as protocol guidance
when it's really a wishlist.

### 5.8 `inspiration_20260510.md` — many "steal X" recommendations are opinionated

A2A's `INPUT_REQUIRED` / `AUTH_REQUIRED` task states (slice 1 §2 item
2, lines 39): agent-shaped. The protocol doesn't know about humans-in-
the-loop *as a protocol concern*; that's a node concern. LangGraph's
per-key reducers (§3 item 1, line 53): workflow-shaped. Phoenix's
`join/3` callback as "a single chokepoint" (§13 item 2): channel-
shaped. The scout doesn't tag any of these `[OPINIONATED]` and treats
them as direct candidates for stealing. Every `steal` recommendation
should be re-checked against the constraint.

### 5.9 `security_audit_20260510.md` patch direction for V-04

Already covered in §2.6. The mention of `approval_node` by name in a
proposed protocol-layer guard is the cleanest example of opinion in a
security patch — which is the *one* place where opinion-leak is most
likely to ship by accident, because security urgency overrides
constraint discipline.

### 5.10 `synthesis_20260510.md` §3 — promote `/v0/admin/*` to protocol or demote dashboard?

§3 point 1 (lines 47–48) frames the binary correctly. Then the rest of
wave 1 picks both: PRD HR-15 *shrinks* `/v0/admin/*` while
dashboard_node_v2 *expands* protocol with `core.*` self-surfaces. That
is the leak in motion across two docs. **Either /v0/admin/* is
protocol (document it in PROTOCOL.md, freeze the surface, port to
BEAM) or it is implementation (delete it, replace with mesh-invoke
against generic core surfaces).** Wave 1 picked "both, simultaneously,
under different worker hats."

---

## 6. What round 2 should prove that round 1 missed

A prioritised punch list. Each item is a measurable artifact, not a
vibe.

1. **A canonical-JSON cross-language golden vector test, checked in.**
   ~30 lines of test data, ~30 lines of harness per implementation.
   Required before any architecture-comparison number can be trusted.
2. **A real V-03 patch that covers respond-path replay.** Plus a unit
   test that fires a captured response twice and asserts the second
   one is rejected. The current patch is a "DO NOT APPLY — proposal."
3. **A real test of V-06 queue eviction.** Slow consumer + 1025
   producers, asserts (a) bounded queue does not OOM, (b) `pending`
   slot is cleaned up on QueueFull, (c) repeated overflow evicts the
   consumer. The hardening doc shipped a unit-level assertion of (a)
   only.
4. **Bimodal latency analysis at c=64.** The B2 histogram has two
   visible modes; round 2 needs a flame graph or a strace identifying
   what's switching between them. Until known, the throughput cap
   claim is contingent.
5. **Cold-start numbers for Elixir, with std dev, n≥100.** "Seconds"
   isn't a number. Same for Rust and Go re-baselined under the same
   methodology.
6. **An audit-log-on-slow-disk benchmark.** Measure `state.audit_lock`
   contention when the log writer is on a 50 MB/s SSD or an SMB share.
   Almost certainly the next surprising bottleneck.
7. **A supervisor microbenchmark.** Spawn → register latency, restart
   storm recovery time, `ensure_running` on the hot path overhead.
   HR-12 ships zero performance evidence today.
8. **A real fork-test attempt.** Pick a non-agent product (a
   deployment-feed pub/sub, a sensor data fan-in, anything that isn't
   "AI agents talking to each other") and try to design it on top of
   `core/` + `node_sdk/`. Document every place the protocol felt
   wrong. The PRD A8 fork test is a checklist; round 2 should
   *execute* it once.
9. **A list of every `[PROTOCOL]` recommendation in wave 1, re-tagged
   under a stricter rubric.** Specifically re-evaluate:
   `dashboard_node_v2`'s six self-surfaces, HR-9 implicit
   `_capabilities` edge, HR-10 caveat shape, HR-12 `on_demand`
   strategy, the `core` reserved id in the validator. Round 2 should
   produce a "what's actually generic" residue.
10. **A SSE-backpressure benchmark across all four prototypes.** Slow
    consumer test on Python, Elixir, Rust, Go. Ground the V-06 / queue
    bound discussion in measurement instead of intuition.
11. **A real `Last-Event-ID` design.** Per-stream monotonic sequence
    numbers (not ISO timestamps), drop-detection on the consumer side,
    a bounded-but-honest replay buffer. The current design in
    `sse_consolidation.md` silently drops and mis-resumes.
12. **A working `/v0/admin/manifest` round-trip test for malformed
    YAML rollback.** Synthesis §4 flagged it; nobody shipped it.
13. **An `_env.sh` migration plan for V-08.** What does an existing
    deployment do on first boot after the patch lands? The hardening
    doc deferred V-08 explicitly; round 2 should ship the sequence.
14. **A documented contract for `core/core.py`'s SSE loop.** Either
    consolidate it under `node_sdk/sse.py` (and accept the protocol-
    boundary risk) or write the contract down so the consolidation's
    drop-on-full / heartbeat / reconnect semantics are unambiguous
    when the BEAM rewrite has to match them.
15. **A test that catches "this is opinion in disguise."** The
    biggest missing artifact. A reviewer (or a CI lint) that asks of
    every protocol-layer change: *"if I deleted every node and the
    dashboard, would this still feel right?"* — and fails the change
    if the answer is no. This is the only mechanical defense against
    the leaks documented in §5.

---

The wave-1 outputs got a lot right: the security audit found 20 real
issues, the LOC parity numbers in the architecture compare are
defensible, the SSE consolidation is a real refactor, the validator
landed with 19 tests. But the constraint discipline slipped in
exactly the place it most matters — when an opinionated layer
prescribed shape to the protocol layer and nobody pushed back hard
enough. Round 2's job is to push back, with evidence, before the
v1 PRD freezes the wrong surface area into protocol bedrock.
