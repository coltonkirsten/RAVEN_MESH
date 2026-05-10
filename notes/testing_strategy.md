# RAVEN_MESH Testing Strategy

**Author:** Worker (test-strategy audit)
**Date:** 2026-05-10
**Status:** Proposal — informs Wave 2 / v1 test plan
**Coverage measurement:** `pytest --cov` run on this branch (pytest-cov was not installed; installed for the audit, then ran the suite). Result: **145 tests pass in ~22 s, 43% line coverage** across `core/`, `node_sdk/`, and `nodes/`.

This document is structured around the **PROTOCOL_CONSTRAINT**: tests are split into two strata. The protocol-layer suite is what every reimplementation of RAVEN_MESH (in Go, Rust, TS) must pass to be called a conforming Core. The opinionated-layer suite is specific to today's nodes and dashboard and may churn freely as those products evolve.

Every recommendation below is tagged **[PROTOCOL]** or **[OPINIONATED]**. If a recommendation could land in either layer, I default to pushing it down per §4 of `PROTOCOL_CONSTRAINT.md`.

---

## 1. Current state — measured

### 1.1 Test inventory

| File | Tests | Layer | Subject |
|---|---|---|---|
| `test_envelope.py` | 12 | **PROTOCOL** | HMAC sign/verify, canonical, schema, register/invoke 401s |
| `test_protocol.py` | 10 | **PROTOCOL** | The 10 PRD demo flows: register, invoke, ACL deny, deny-by-human, cross-language node |
| `test_admin.py` | 13 | **PROTOCOL** | `/v0/admin/{state,stream,reload,invoke,node_status,…}`, rate limit, ADMIN_TOKEN boot guard, node-queue bound |
| `test_manifest_validator.py` | ~20 | **PROTOCOL** | Manifest-schema validation, ID rules, edge resolution |
| `test_supervisor.py` | ~30 | **PROTOCOL** | spawn/stop/restart, restart strategies (permanent / transient / temporary / on_demand), exponential backoff, drain, metrics |
| `test_supervisor_integration.py` | 10 | **PROTOCOL** | The same supervisor surface end-to-end through `/v0/admin/{spawn,stop,restart,reconcile,drain,processes,metrics}` |
| `test_kanban_node.py` | ~25 | **OPINIONATED** | kanban capability surfaces, persistence, UI gate |
| `test_voice_actor.py` | ~10 | **OPINIONATED** | Surface registration, schema validation, no-API-key graceful degradation |
| `test_mesh_db_node.py` | ~5 | **OPINIONATED** | audit-log query node (lives under `experiments/`) |
| `test_nexus_agent.py` / `test_nexus_agent_isolated.py` | ~20 | **OPINIONATED** | Agent + MCP bridge wiring |

**Total: 145 tests, 22 s wall-clock.** Fast enough to run on every commit.

### 1.2 Measured line coverage (`pytest --cov`)

| Module | Coverage | Layer | Note |
|---|---|---|---|
| `core/core.py` | **73%** | **PROTOCOL** | Hot path solid; misses cluster around process-supervisor wiring, SSE error branches, lines 971–1046 (boot/CLI), and a handful of admin error returns |
| `core/manifest_validator.py` | **89%** | **PROTOCOL** | Strong |
| `core/supervisor.py` | **86%** | **PROTOCOL** | Good. Gaps: a few error branches (266–293, 562–578) — uncovered restart-race and SIGKILL fallback paths |
| `node_sdk/__init__.py` | **81%** | **PROTOCOL** | Reasonable |
| `node_sdk/sse.py` | **28%** | **PROTOCOL** | **Major gap** — reconnect / backoff / line buffering completely untested |
| `nodes/kanban_node/` | 67% | OPINIONATED | OK; UI handlers under-tested |
| `nodes/voice_actor/voice_actor.py` | 17% | OPINIONATED | Most paths gated on `OPENAI_API_KEY`, but the no-key graceful path is the only one tested |
| `nodes/voice_actor/audio_io.py` | 13% | OPINIONATED | Untested |
| `nodes/voice_actor/realtime_client.py` | 28% | OPINIONATED | Only decode-audio-delta tested |
| `nodes/nexus_agent/` (and `_isolated/`) | 25–58% | OPINIONATED | Web server, CLI runner, MCP bridge largely untested |
| `nodes/cron_node/` | **0%** | OPINIONATED | No tests |
| `nodes/approval_node/` | **0%** | OPINIONATED | No tests |
| `nodes/dummy/*` | **0%** | OPINIONATED | Demo-only; could be deliberate |
| `nodes/human_node/` | **0%** | OPINIONATED | No tests |
| `nodes/webui_node/` | **0%** | OPINIONATED | No tests |
| `nodes/ui_visibility.py` | 36% | OPINIONATED | Helper untested in most call paths |
| `dashboard/` (React) | **0%** | OPINIONATED | No JS test framework configured at all |

**Headline:** the protocol layer is in good shape (~80%+ on most modules), the opinionated layer is patchy, and the dashboard has zero automated tests.

---

## 2. Gaps — what is NOT tested

### Protocol-layer gaps **[PROTOCOL]**
1. **Envelope ordering / FIFO under load.** The protocol guarantees per-pair FIFO, but no test fires N envelopes concurrently and asserts order on the receive side.
2. **Replay protection.** `test_envelope.py` proves a tampered payload fails verify, but **never tries a valid envelope replayed twice**. Today the envelope schema has no nonce/timestamp-skew check; the test gap also masks the protocol gap.
3. **Slow / disconnected consumer.** `test_admin.py::test_node_queue_is_bounded` probes the queue cap directly but does not drive the bound through real SSE traffic — the actual `denied_queue_full` envelope path is uncovered.
4. **SSE reconnect** in `node_sdk/sse.py`: 28% coverage. Backoff, partial-line buffering, server-restart resume — none exercised.
5. **Manifest hot-reload race.** `/v0/admin/reload` is tested for happy path; reload **while traffic is in flight** is not.
6. **Supervisor crash-recovery.** We test crash-restart of children. We do **not** test "Core process dies → children outlive Core → reconciler picks them up on restart" (or alternative: orphan children get reaped). Behavior here is currently undefined; tests would force us to define it.
7. **Cross-language conformance.** Step 10 (external stdlib node) covers one happy path. There is no parameterized conformance battery a Go/Rust reimplementation could run.
8. **Authn/authz fuzzing.** Spec-compliant signed envelopes from unknown senders, ID spoofing (claim to be `tasks` while signing with `voice_actor`'s secret), oversized payloads, malformed JSON — uncovered.
9. **Audit-log integrity.** We assert events are routed; we don't assert the audit log is append-only, signature-checkable, or survives a crash mid-write.
10. **Admin rate-limit fairness.** `test_admin_rate_limit_returns_429` checks the bound but not per-token-bucket isolation.

### Opinionated-layer gaps **[OPINIONATED]**
- Five node packages (`cron_node`, `approval_node`, `human_node`, `webui_node`, `dummy/*`) have no tests at all.
- voice_actor's actual realtime loop, audio I/O, and tool-dispatch paths are untested (network / hardware-bound — see §4 below).
- Dashboard has no test runner — no Vitest/Playwright wiring.

---

## 3. Proposed taxonomy for v1

Four tiers. Each test belongs to exactly one tier and is annotated with its layer.

### 3.1 Unit tests
**Goal:** function-level invariants, no I/O, no subprocess.
**Examples (PROTOCOL):** `canonical()`, `sign()`, `verify()`, `_backoff_seconds()`, `validate_manifest()`, ACL edge resolution.
**Examples (OPINIONATED):** `KanbanBoard.move_card`, `realtime_client._decode_audio_delta`, `ui_visibility` rule helpers.
**Budget:** must run in <2 s total. No event loop unless the unit is async-only.

### 3.2 Integration tests
**Goal:** real Core process (in-process via `make_app`) + real `MeshNode` clients + real subprocesses for the supervisor. No external network.
**Examples (PROTOCOL):** all of `test_protocol.py`, `test_admin.py`, `test_supervisor_integration.py`. The "external_node via stdlib HTTP" test in step 10 also lives here.
**Examples (OPINIONATED):** `test_kanban_node.py`, `test_voice_actor.py` happy-path-without-API-key.
**Budget:** ~30 s. Currently we are at 22 s.

### 3.3 End-to-end (e2e) tests
**Goal:** real subprocesses for nodes + real Core + real dashboard via Playwright. Today this tier doesn't exist.
**Examples (OPINIONATED):** boot the demo manifest with `enable_supervisor=True`, drive the kanban board through the dashboard UI, assert envelopes show up in `/v0/admin/state`. **One e2e per shipped product story** is the right ceiling — this tier is expensive.
**Budget:** ~2–3 min, gated to PR + nightly, not every commit.

### 3.4 Chaos tests **[PROTOCOL]**
**Goal:** prove protocol invariants under failure. New tier.
**Examples:**
- Kill the supervisor mid-spawn; assert children are either fully started or absent (no half-states).
- Drop the SSE channel of a node mid-invocation; assert the requesting node sees `denied_unreachable` (or whatever the spec says) within a bounded time.
- Restart Core; assert nodes reconnect within N seconds and pick up traffic.
- Fill the per-node delivery queue; assert overflow surfaces as `denied_queue_full` and that recovery happens once the consumer drains.
- Concurrent invokes from many nodes to one node; assert FIFO per (from→to) pair.
**Budget:** ~30 s, runs on PR, not every commit. Lives in `tests/chaos/`.

---

## 4. How to test mesh-specific properties

### 4.1 Envelope ordering **[PROTOCOL]**
Add `tests/test_ordering.py`:
- Spawn one capability node that records `(payload["seq"], received_at)` per envelope.
- One actor fires N=200 invocations sequentially with seq=0..199 (the cheap case — already implied by request/response).
- One actor fires N=200 invocations with `asyncio.gather` (the real test). The protocol must still deliver them in **submission order on the wire** for a single sender → single receiver. Assert seq is monotonic on the receiver. If not, that is a protocol bug, not a test bug.
- Add a multi-sender variant: 5 senders × 40 invocations. Assert per-sender FIFO; cross-sender order is undefined and we explicitly test that we do NOT depend on it.

### 4.2 Signature replay **[PROTOCOL]**
Today the envelope has `id`, `correlation_id`, `from`, `to`, `kind`, `payload`, `timestamp`, `signature`. There is no documented anti-replay. Two options, pick one and write the test:
- **Option A (cheap, recommended):** Core keeps a bounded LRU of seen `(from, id)` pairs and rejects duplicates with `denied_replay`. Test: send the same signed envelope twice — second is denied.
- **Option B (stricter):** require `timestamp` within ±N seconds of Core wall clock; reject otherwise. Test: clock-skew envelope rejected with `denied_clock_skew`.
This is a **protocol** decision and the test belongs in `test_envelope.py`.

### 4.3 Supervisor crash-recovery **[PROTOCOL]**
The supervisor itself is well-tested in isolation. The missing test is **what happens when Core (the supervisor's host) dies**:
1. Boot Core with `enable_supervisor=True`, spawn a long-lived child.
2. `os.kill(core_pid, SIGKILL)`.
3. Restart Core. Inspect `/v0/admin/processes` and verify the contract — orphan reaped, or orphan adopted, or orphan refused-to-adopt-and-spawn-fresh. **Right now the contract is unspecified**, which is itself a finding.

This test is in `tests/chaos/test_core_restart.py`. It will fail until the contract is decided; that is the point.

### 4.4 ACL deny-by-edge **[PROTOCOL]**
Currently covered (`test_step_8_denied_no_relationship`). Extend to assert:
- Audit log records every denied attempt (already true in spirit; add an explicit assertion).
- Per-pair deny rate is exposed in `/v0/admin/metrics` so observability of attack patterns is part of the protocol contract.

### 4.5 Manifest hot-reload safety **[PROTOCOL]**
Add: while a slow `tasks.list` invocation is in flight, swap the manifest to remove the `voice_actor → tasks.list` edge. The in-flight invocation must complete (already-routed envelopes are committed). New invocations must be denied.

---

## 5. Concrete plan to reach 90% line coverage

### 5.1 Protocol layer to **≥95%** **[PROTOCOL]**
Currently 73% (core), 86% (supervisor), 89% (manifest_validator), 28% (sse). Headline is `node_sdk/sse.py`.

| Add | Module | Effort |
|---|---|---|
| `test_sse_reconnect.py` — kill Core, bring it back, assert client reconnects within N s | `node_sdk/sse.py` | M |
| `test_sse_partial_line.py` — feed split SSE frames, assert single envelope decoded | `node_sdk/sse.py` | S |
| `test_envelope_replay.py` — replay rejected | `core/core.py` (after spec decision) | M |
| `test_ordering.py` — concurrent FIFO | `core/core.py` | M |
| `test_manifest_reload_inflight.py` — reload during traffic | `core/core.py` | M |
| `test_supervisor_orphan.py` — chaos, see §4.3 | `core/supervisor.py` (and the boot path) | L |
| Fill 1046-line `core/core.py` boot/CLI tail (lines 971–1046) with one CLI integration test | `core/core.py` | S |

Doing all of the above on top of the existing 73/86/89 brings the protocol layer to comfortably >95%.

### 5.2 Opinionated layer to **≥85%** **[OPINIONATED]**
Currently 0–67% depending on node. Plan:

| Node | Action | Tier | Effort |
|---|---|---|---|
| `cron_node` | Write `test_cron_node.py`: schedule expression parsing, edge-trigger semantics, surface schema | unit + integration | M |
| `approval_node` | Write `test_approval_node.py`: approve/deny pass-through, audit, timeout | integration | M |
| `human_node` | Write `test_human_node.py`: inbox API, surface gating | integration | M |
| `webui_node` | Write `test_webui_node.py`: surface registration, page render smoke (httpx GET) | integration | S |
| `dummy/*` | Decide: are these tests fixtures or shipped nodes? If fixtures, exclude from coverage. If shipped, write minimal smoke tests. | — | S |
| `voice_actor` | Add a **mock-OpenAI** Realtime fixture. Today most coverage gaps live behind `OPENAI_API_KEY`; mocking the WebSocket lets us drive the full state machine without cost | integration | L |
| `kanban_node` UI handlers (lines 404–459) | Add `test_kanban_ui.py` covering the dashboard-facing HTTP handlers | integration | S |

### 5.3 Dashboard **[OPINIONATED]**
Today: 0 tests, no test runner.
Plan:
1. Add Vitest + React Testing Library; one component test per page (smoke level).
2. Add Playwright with a single e2e: boot Core + supervised demo manifest in a fixture, hit the dashboard, assert process list renders.
3. Treat the React layer as **opinionated** — its tests can churn.

### 5.4 Quantified path to 90%

Using current numbers (4426 stmts, 2526 missed, 43%):
- Filling the four 0%-coverage node packages (`cron`, `approval`, `human`, `webui`): ~550 stmts → +12% absolute.
- Mock-OpenAI for voice_actor (covers ~600 of the 880 missed stmts): +14%.
- Filling `node_sdk/sse.py`: +1%.
- nexus_agent web/runner/bridge: +6%.
- Misc fill (kanban UI, ui_visibility, manifest_validator dregs): +5%.

That math lands at **~80–82%**. To clear 90% you also need:
- Excluding `nodes/dummy/*` and any "experiment" code from `--cov` (legitimately, they are fixtures).
- Or: deleting code that exists only because it was scaffolded and isn't shipped.

**Recommendation:** target **90% on `core/` + `node_sdk/`** (the protocol moat) and **80% on `nodes/`** (the opinionated layer). A single global 90% bar pushes us toward over-testing dummy code; per-layer bars keep the right things rigorous.

---

## 6. Test layout

Reorganize from one flat `tests/` into:

```
tests/
  protocol/              # PROTOCOL — must always pass; portable across reimpls
    test_envelope.py
    test_protocol.py
    test_admin.py
    test_manifest_validator.py
    test_supervisor.py
    test_supervisor_integration.py
    test_ordering.py             # NEW
    test_replay.py               # NEW
    test_sse.py                  # NEW
  chaos/                 # PROTOCOL — failure-mode invariants
    test_core_restart.py         # NEW
    test_slow_consumer.py        # NEW
    test_manifest_reload_race.py # NEW
  nodes/                 # OPINIONATED — current node implementations
    test_kanban_node.py
    test_voice_actor.py
    test_cron_node.py            # NEW
    test_approval_node.py        # NEW
    test_human_node.py           # NEW
    test_webui_node.py           # NEW
    test_nexus_agent.py
    test_nexus_agent_isolated.py
  e2e/                   # OPINIONATED — Playwright + dashboard
    test_dashboard_smoke.py      # NEW
  conftest.py
```

The directory split is the **load-bearing** change: it makes the layer separation visible. Tests under `tests/protocol/` are the spec. Tests under `tests/nodes/` and `tests/e2e/` are product, and may be deleted wholesale when a node is replaced — without that ever causing a protocol regression.

CI pipelines:
- **every commit**: `tests/protocol/` + `tests/nodes/` (≤30 s)
- **every PR**: + `tests/chaos/` + `tests/e2e/` (≤3 min)
- **nightly**: + a 5-minute soak / load run that fires 100k envelopes through Core and asserts no crash, no leak

---

## 7. Layer-leak audit on this proposal

Per `PROTOCOL_CONSTRAINT.md` §6: did I leak opinion into the protocol?

- The proposed `tests/protocol/` covers envelope shape, ACL, signing, supervisor mechanics, and manifest validation. None of these reference kanban/voice/dashboard. ✅
- The replay-protection test depends on a not-yet-made spec decision; the test belongs in `tests/protocol/` only **after** the decision is made. Until then it lives in `tests/chaos/` as a documented gap. ✅
- The supervisor crash-recovery test exposes an undefined contract; that's a feature, not a bug — protocol tests should drive specs, not the other way around. ✅
- Dashboard tests, voice-actor mock-OpenAI, kanban UI — all in `tests/nodes/` or `tests/e2e/`. ✅

Nothing in the protocol tier names a specific node. A fork that throws away every node and the dashboard would still be expected to keep `tests/protocol/` green. That is the correctness criterion.

---

## 8. Summary

- **State:** 145 tests, 22 s, 43% coverage. Protocol layer is well-tested (~80%+ on core modules), opinionated layer is patchy (0–67%), dashboard is untested.
- **Big rocks:** SSE client (`node_sdk/sse.py`), four 0%-coverage node packages, voice_actor's network-bound paths, dashboard.
- **Net new tiers:** introduce `tests/chaos/` for protocol-invariant failure-mode tests; introduce `tests/e2e/` for dashboard.
- **Mesh-specific properties to lock down:** ordering (FIFO per-pair), replay (after spec decision), supervisor crash-recovery (after spec decision), slow-consumer overflow.
- **Coverage targets:** 90% on `core/` + `node_sdk/`, 80% on `nodes/`. A single global 90% bar is the wrong instrument because it ignores the layer split.
- **Layout:** split `tests/` into `protocol/`, `chaos/`, `nodes/`, `e2e/`. The split is what makes the protocol/opinionated boundary enforceable in CI.
