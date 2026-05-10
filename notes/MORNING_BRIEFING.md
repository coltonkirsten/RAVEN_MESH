# Morning Briefing — 2026-05-10

**Audience:** Colton, with coffee, ~15 minutes.
**Author:** night-shift synthesis worker
**Constraint observed:** every recommendation tagged `[protocol]`, `[opinionated]`, or `[research]` per `notes/PROTOCOL_CONSTRAINT.md`. Substitution test ("could a fork delete every node + dashboard and still build a different product on this protocol?") used as the line.

---

## 1. TL;DR

- Three protocol-layer security fixes **shipped tonight** (admin-token-no-default, token-bucket admin rate-limit, bounded per-node queues). 125 tests green. The biggest unshipped wins are wiring the new manifest validator into `load_manifest` and adding HMAC replay protection to `/v0/respond`.
- The bold "make the dashboard a real mesh node" idea survives wave 2, but **only if you rewrite the proposed `core.*` self-surfaces from a protocol-first frame**, not a UI-feature-list frame. Wave 1's first draft leaked.
- Elixir is still the v1.x rewrite trigger, **not the v1 trigger**. ~80% of v1 PRD hard-requirements (HR-1..HR-14) are reachable inside Python in 1–2 weeks.

*(99 words.)*

---

## 2. What got built tonight

### Protocol-layer artifacts (the unopinionated mesh)

- **`core/core.py` — three security hardenings landed.** Admin token no longer has a default and is now header-only (no `?token=` query parameter); admin endpoints sit behind a 60-req/min token-bucket with burst 20; per-node delivery queues are bounded at `maxsize=1024` and overflows emit `denied_queue_full` audit events. 125 tests passing.
  Source: `notes/security_hardening.md`.
- **`core/manifest_validator.py` — landed with 19 tests.** Strict-on-remote-write design. **Not yet wired into `load_manifest`.** Two-stage rollout proposed: warnings-only opt-in → strict default.
  Source: `notes/manifest_validation_design.md`.
- **`node_sdk/sse.py` — `SSEHub` extracted from inline copies.** 2 of 8 call sites migrated (`nexus_agent`, `kanban_node`). 6 left, plus `core/core.py` itself was deliberately excluded — that exclusion is itself a protocol-layer gap.
  Source: `notes/sse_consolidation.md`.
- **Cleanup pass.** 8 dead imports removed across `core/`, `node_sdk/`, and `nodes/`. Public-API docstrings added to `node_sdk/__init__.py` and `core/supervisor.py`. Pyflakes clean. 145 tests pass before/after. No layering violations found in `core/` or `node_sdk/`.
  Source: `notes/cleanup_pass.md`.

### Opinionated-layer artifacts (nodes, dashboard, product)

- **`dashboard_node_v2.md` proposal.** Refactor of synthesis §6 — moves the dashboard out of `dashboard/` and into `nodes/dashboard_node/`, talking to the core through six new `core.*` self-surfaces (`core.state`, `core.audit_stream`, `core.set_manifest`, `core.reload_manifest`, `core.invoke_as`, `core.lifecycle.*`, `core.processes`). Six-PR migration plan. Not yet implemented.
  Caveat: the proposal *itself* is a protocol expansion. See §6.
- No new node logic shipped tonight. Existing nodes (`kanban`, `voice_actor`, `nexus_agent`, `nexus_agent_isolated`) untouched.

### Research / notes

- **`wave_1_critique.md`** — the most important doc tonight. Brutal review of wave 1; cataloged 10 layer-leak items and several methodology errors in the wave 1 benchmark. Every other note should be read with this one open.
- **`v1_prd_draft.md`** — HR-1..HR-19. HR-1..HR-14 protocol-layer; HR-15..HR-19 opinionated. A1..A10 acceptance tests, including A8 fork test.
- **`security_audit_20260510.md`** — 20 vulnerabilities V-01..V-20 across protocol & opinionated layers.
- **`security_postmortem.md`** — gap matrix combining audit + validator note; three layered diffs for admin trust root, secret derivation, bounded queue.
- **`architecture_compare.md`** — side-by-side of 4 prototypes (Python incumbent 1164 LOC, Elixir 730 LOC, Rust 1361 LOC / 23ms cold start, Go 1215 LOC, NATS-pivot 511 LOC SDK). Recommendation: v1 stays Python, Elixir is the rewrite trigger, Go is the backup, Rust reserved for nodes, NATS as transport-only.
- **`benchmark_results_20260510.md`** — protocol round-trip p50 0.44ms / p99 0.49ms; ~2.2k rps single-client; ~4.8k rps c=64; ~7× HTTP floor cost; ~420ms cold boot.
- **`capability_graph/MODEL.md`** — Datalog formalization of allow-edges; mappings to OCapN / seL4 / Macaroons / Meadowcap. Five weaknesses, four extensions.
- **`migration_path.md`** — 6-week Python→Elixir plan, 2026-05-11 → 2026-06-21.
- **`testing_strategy.md`** — 145 tests, 22s wall, 43% line coverage. Coverage targets 90% protocol / 80% opinionated.
- **`operational_playbook.md`** — 6 runbooks, layer-tagged.
- **`mesh_only_ideas_20260510.md`** — 15 capability ideas only the mesh can offer; pick is #11 (Mesh-as-Database) + #2 (Provenance Stamps) as a synergistic slice.
- **`docs_audit.md`** — audit of `README.md`, `docs/PROTOCOL.md`, `docs/PROTOTYPE.md`. Drafts of replacement README, PROTOCOL, ARCHITECTURE.md. Single recurring failure across all three: protocol/opinionated split is invisible.

---

## 3. Key insights (5 non-obvious findings)

### 3.1 SSE `Last-Event-ID` resume is broken because event IDs use ISO timestamps, which are *not monotonic*.

ISO-8601 strings sort lexicographically the same as chronologically *only if the timezone offset is identical and the precision is identical for every event*. The current implementation uses host wallclock time, which can move backwards under NTP correction or DST, and can collide at sub-millisecond bursts. The wire format diff added an `id:` line, but the IDs themselves are unfit for the resume semantics they imply.
Source: `notes/sse_consolidation.md` ("ISO timestamp event_ids are not monotonic — flagged broken resume semantics").
**Why this matters now:** if we ship `Last-Event-ID` in v1 looking like a contract, every consumer will write resume code against a guarantee we can't actually deliver. Better to either (a) move to a monotonic counter per stream, or (b) explicitly document `Last-Event-ID` as best-effort with collisions and reordering possible.

### 3.2 The benchmark's "no hiccups" claim at c=64 was wrong — latency was bimodal.

The wave 1 bench reported clean latency curves through c=64, but the histogram showed two clusters (~8.5ms and ~12.4ms). Wave 2 reads this as the Core-saturation knee arriving earlier than the rps number suggests. The 4.8k rps headline number is the single-client-pool throughput, not a mesh-wide capacity claim — and the supervisor was *off* during that run, and `MESH_ADMIN_RATE_LIMIT=0` was set for the reload bench. So:
- The published 4.8k rps overstates real protocol throughput by an unknown factor (supervisor off = no wakeup overhead, no child accounting).
- The "knee at c=64" finding is methodologically softer than the doc presents it.
Source: `notes/wave_1_critique.md` and `notes/benchmark_results_20260510.md`.
**Why this matters now:** any "is Python fast enough?" decision that cites this benchmark needs a re-run with supervisor on, rate-limit on, and the histogram inspected, not just the percentiles.

### 3.3 280ms of the 420ms cold boot is Python startup × 3, not protocol cost.

The cold-boot number sounds like a protocol design problem ("the mesh takes 420ms to come up"). It isn't. Three Python interpreter starts dominate. The protocol-attributable portion is ~140ms.
Source: `notes/benchmark_results_20260510.md`.
**Why this matters now:** if you justify the Elixir rewrite primarily on cold-boot, you're justifying it on a 280ms problem that's an interpreter choice, not a design choice. The real Elixir wins (BEAM supervision, hot code reload, soft-realtime scheduler) are stronger arguments and survive scrutiny better. Lead with those.

### 3.4 The smallest first step on the capability graph is **5.2 caveats**, not 5.1 delegation.

`capability_graph/MODEL.md` formalizes allow-edges as Datalog and lists four extensions:
- 5.1 delegation (cap-passing)
- 5.2 caveats (constrained edges)
- 5.3 time bounds
- 5.4 `_capabilities` introspection surface

Caveats give us 80% of Macaroons-style "I can call X but only with this filter applied" with no schema migration — they're additive metadata on existing edges. Delegation forces an identity rethink (who is "the bearer"?) and an audit-log redesign (who actually invoked?). 5.2 first; 5.1 only if/when a real product need (third-party agents calling on your behalf) appears.
Source: `capability_graph/MODEL.md` §5.
**Why this matters now:** wave 1's HR-10 in the v1 PRD specifies *kanban-shaped* caveats ("only allow `move_card` if `from_column != done`"). That's a layer leak (§6) — but the underlying *mechanism* is the right protocol primitive, just the example needs delaminating.

### 3.5 The substitution test exposed `synthesis_20260510.md §6` as a UI-feature-list pretending to be a protocol spec.

Wave 1's §6 ("dashboard as a node") proposed six new `core.*` self-surfaces. Wave 2 noticed that the surfaces were derived bottom-up from "what does the dashboard React app currently fetch?" rather than top-down from "what does any introspecting node need?" Three of the six surfaces (`core.invoke_as`, `core.set_manifest`, `core.reload_manifest`) are admin actions in node clothing — they're already exposed as `/v0/admin/*` HTTP endpoints, and re-exposing them through the mesh adds attack surface for marginal symmetry gain. `dashboard_node_v2.md` is the cleaned-up version, but it does not fully undo the leak (see §6).
Source: `notes/wave_1_critique.md` §"layer leaks", item 1.
**Why this matters now:** the migration plan to Elixir (`migration_path.md`) is gated on this refactor landing first. If the refactor is wrong, the migration is wrong.

---

## 4. Recommendations (ordered by impact × cheapness)

### R1. Wire `manifest_validator` into `load_manifest` in warnings-mode. `[protocol]`
**What:** call `validate_manifest()` on every load; log warnings; do not yet reject. After two weeks of clean logs, flip to strict.
**Why high impact:** every other protocol hard-requirement that depends on a *correct* manifest (allow-edge ACL, replay protection scoping, queue accounting) silently degrades when the manifest is malformed. We have the validator and tests; we just haven't called it.
**Effort:** ~half a day. Already designed in `notes/manifest_validation_design.md` §"Stage 1".
**Risk:** none in warnings-mode.

### R2. Add HMAC replay protection to `/v0/respond`. `[protocol]`
**What:** the existing replay protection (timestamp ±60s + nonce LRU) covers `/v0/register` and `/v0/invoke` but not `/v0/respond`. V-04 in the audit.
**Why high impact:** an attacker who captures one `respond` envelope can replay it indefinitely to forge node responses. This breaks the trust model the rest of the protocol assumes.
**Effort:** ~1 day. Same code path as register/invoke; just extend coverage.
**Caution — layer leak in the audit's patch direction:** `notes/security_audit_20260510.md` V-04 names `approval_node` as the motivating attack surface. That's a node-shaped justification for a protocol-shaped fix. Land the fix; rewrite the rationale to be node-agnostic.

### R3. Fix `Last-Event-ID` monotonicity. `[protocol]`
**What:** replace ISO-timestamp event IDs with a per-stream monotonic counter (or a `(epoch_ms, counter)` tuple). Document resume semantics as exactly-once-or-skip, not exactly-once.
**Why:** §3.1. We're shipping a guarantee we can't keep.
**Effort:** ~1 day in `node_sdk/sse.py`; small migration for existing subscribers.

### R4. Finish the SSE consolidation. Migrate `core/core.py` and the remaining 5 node call sites through `node_sdk.sse.SSEHub`. `[protocol]`
**What:** 6 of 8 SSE sites still inline their own logic. `core/core.py`'s exclusion is the most consequential — it means the protocol's own SSE behavior diverges from what nodes ship.
**Why:** prevents bug-fix drift. The `Last-Event-ID` fix in R3 lands in one place if and only if R4 is done first.
**Effort:** ~2 days.

### R5. Move the dashboard to `nodes/dashboard_node/`, but **rewrite the proposed `core.*` surfaces from a protocol-first frame first**. `[opinionated, with `[protocol]` prerequisite]`
**What:** the substitution-test re-derivation. Of the six surfaces in `dashboard_node_v2.md`:
- `core.state` — keep, it's introspection. *Required* by HR-9 anyway.
- `core.audit_stream` — keep. Already a thing in different clothing; this just gives it a node-shaped name.
- `core.processes` — keep, but trim. Process listing is introspection. Process *control* is admin.
- `core.lifecycle.{spawn,stop,restart,reconcile}` — **drop from protocol**. These are admin actions. Keep the dashboard as a privileged client of `/v0/admin/*` endpoints.
- `core.set_manifest` — **drop from protocol**. Same reason.
- `core.reload_manifest` — **drop from protocol**. Same reason.
- `core.invoke_as` — **drop entirely**. Identity-spoofing-as-a-mesh-surface is a class break of the security model. If the dashboard needs to act on behalf of users, that belongs in an *opinionated* identity layer above the protocol.
**Why:** §3.5. Without this trim, the rewrite expands protocol attack surface and breaks the substitution test.
**Effort:** the trim is free. The PR sequence in `dashboard_node_v2.md` is otherwise sound.

### R6. Capability graph: ship 5.2 caveats next, defer 5.1 delegation. `[protocol]`
**What:** see §3.4. Add an optional `caveats` field to allow-edges; evaluate as Datalog filters at edge-check time.
**Why:** smallest first step that buys us most of the security expressiveness.
**Effort:** ~3 days. Reuses the manifest validator infrastructure (R1).

### R7. Stay Python through v1; trigger the Elixir rewrite when 3 of 5 litmus tests trip. `[research → protocol]`
**What:** `architecture_compare.md` proposes 5 litmus tests for the rewrite trigger (sustained throughput ceiling, supervision regressions, hot-deploy demand, cluster size, soft-realtime SLAs). Adopt them; don't rewrite proactively. ~80% of HR-1..HR-14 are reachable in Python in 1–2 weeks.
**Why:** Elixir wins are real but not v1-blocking. §3.3 — the cold-boot argument is weaker than it looks.
**Effort:** ~zero now; preserve `notes/migration_path.md` as the recipe for when the trigger fires.

### R8. Patch `notes/operational_playbook.md` §3 step 1 to drop the phantom `details.drain` convention. `[notes]`
**What:** the runbook prescribes a drain field that does not exist in the current envelope schema. Wave 2 critique flagged this. Either implement the field (then it's a `[protocol]` change) or remove it from the playbook.
**Why:** runbooks become dangerous when they prescribe nonexistent fields — operators trust them.
**Effort:** 5 minutes (delete) or 1 day (implement). Recommend delete; the supervisor's `can_accept` / `begin_work` / `end_work` already cover the drain semantics without operator-visible fields.

### R9. Pre-existing `aiohttp.NotAppKeyWarning` deprecation (144 warnings). `[protocol]`
Not urgent, but easy. Lowest priority on this list.

---

## 5. Open questions (need Colton's call)

### Q1. Is the manifest validator's strict-on-remote-write rule *always-on* for security-relevant fields, regardless of stage?
The validator design uses a two-stage rollout (warnings → strict default). But a subset of rules — duplicate `id`, malformed allow-edge, missing required surfaces — protect security invariants. Should those *always* hard-fail, even in stage 1? My read: yes. But that conflicts with "warnings-only opt-in," and the design doc punts.
Source: `notes/manifest_validation_design.md`.

### Q2. Should the HMAC replay window (currently ±60s) be configurable?
The audit treats 60s as a constant. Operators on lossy networks may want 120s. Operators in tight security contexts may want 10s. Configurable means an extra knob to misconfigure. Hard-coded means uniform behavior. Compromise: configurable per-deployment via `MESH_REPLAY_WINDOW_S`, with a hard upper bound of 300s enforced in code.
Source: `notes/security_postmortem.md` open questions.

### Q3. Does the ephemeral-token SDK helper belong in `node_sdk/` (protocol) or in a new `nodes/_lib/auth/` (opinionated)?
The voice-actor uses ephemeral OpenAI Realtime tokens. The pattern (mint short-lived token, hand to client, expire fast) is general. But it's also *very* application-shaped (OAuth-ish flow, expiry semantics, refresh policy). My read: **opinionated**. Keep `node_sdk/` minimal. But it's a judgment call.
Source: `notes/security_postmortem.md` open questions.

### Q4. Should `core` be a reserved manifest `id`?
The validator currently reserves `core` for self-surfaces (echo of synthesis §6). Wave 2 critique flagged this. If we trim §6 per R5, only `core.state` and `core.audit_stream` (and `core.processes`) survive — and those could be exposed as protocol *endpoints* rather than as a manifest-level node. Your call: do we want the symmetry of `core` looking like a node in the manifest, or do we want the protocol's own surfaces to live outside the manifest?
Source: `notes/wave_1_critique.md` and `notes/manifest_validation_design.md`.

### Q5. Is the dashboard refactor (R5) a *hard gate* on the Elixir migration, or can the migration ship Python's current dashboard surface?
`migration_path.md` assumes the refactor lands first. Wave 2 critique calls this a layer leak — the migration is a protocol concern; the dashboard is opinionated. They should be independent. But: re-implementing the *current* dashboard surface in Elixir means re-implementing the leak. So the migration is "easy" only if we either (a) refactor first, or (b) deliberately drop the dashboard from the v1.x scope and rebuild it after Elixir. **Your call.**

### Q6. Acceptance test A8 (fork test): when do we run it for real?
A8 requires deleting every node and the dashboard, building a different product on the protocol, and showing it works. Today, that test would fail because the manifest validator reserves `core`, the operational playbook references `details.drain`, and the security audit references `approval_node`. Should A8 run as part of CI now (as a tripwire for layering regressions), even though it's expected to fail until the leaks above are fixed? My read: yes, mark it `xfail`, fix one leak per week.

### Q7. SSE consolidation: do we accept the wire-format diff (added `id:` line) as a breaking change for v1, or maintain a compatibility shim?
The new `id:` line is harmless to compliant SSE clients but visible in tests. Easier to ship if v1 just declares it. Harder if we promise compatibility with pre-`id:` consumers.

---

## 6. Layering audit

The constraint: "the mesh protocol is unopinionated; the dashboard and the nodes are opinionated layers on top." Below: every place wave 1 or wave 2 leaked product opinion *into* the protocol, or pulled protocol concerns *into* the opinionated layer.

### Leaks of opinion *into* protocol (the more dangerous direction)

- **`synthesis_20260510.md §6`** — six `core.*` self-surfaces derived from the dashboard React app's current fetches. Half are admin actions in node clothing. Status: refined in `dashboard_node_v2.md` but not fully cleaned. Fix: R5 trim.
- **`dashboard_node_v2.md`** — itself fails to undo the leak. Specifically, retains `core.invoke_as`, `core.set_manifest`, `core.reload_manifest`, and the full `core.lifecycle.*` suite as protocol surfaces. Fix: R5 trim.
- **`v1_prd_draft.md` HR-9** — implicit `_capabilities` allow-edge. Treats the introspection surface as if every node implicitly delegates introspection capability to every other node. That's a policy choice (and a noisy one), not a protocol primitive. Fix: make it explicit and opt-in.
- **`v1_prd_draft.md` HR-10** — caveat example is kanban-shaped (`if from_column != done`). The *primitive* is right (5.2 from §3.4); the example needs delaminating. Fix: rewrite the HR with a generic example (e.g., "if `payload.amount < cap`").
- **`v1_prd_draft.md` HR-12** — `on_demand` restart strategy is described in agent-shaped terms ("for on-demand spawn of nexus_agent containers"). The strategy is generic; the description is not. Fix: rephrase agent-agnostic.
- **`migration_path.md`** — gated on `dashboard_node_v2.md` landing. Migration is a protocol concern; the dashboard is opinionated. Fix: decouple per Q5.
- **`core/manifest_validator.py`** — reserves `core` as an id. Echo of the synthesis §6 leak. Fix: per Q4.
- **`notes/security_audit_20260510.md` V-04** — patch direction names `approval_node` as the motivating threat. Protocol fix, opinionated rationale. Fix: rewrite rationale to node-agnostic.
- **`notes/inspiration_20260510.md`** — "steal X" recommendations untagged. Some are protocol primitives (Macaroons-style caveats, OCapN delegation, seL4 capability discipline); some are application patterns. Fix: tag every recommendation with intended layer before any of them turn into a PR.

### Leaks of protocol concerns *into* the opinionated layer

- **`notes/operational_playbook.md` §3 step 1** — prescribes a `details.drain` envelope field that does not exist. Either it's a protocol-layer field (then add it to schema and document) or it's a runbook fiction. Fix: per R8.
- **`core/core.py` (pre-tonight)** — admin token had a default and accepted `?token=` query param. Mixed protocol-level admin gate with developer-experience convenience. Fixed tonight by `notes/security_hardening.md`.

### Suggestion (not a violation)

- `core/supervisor.py:80` docstring uses the words "kanban, voice, dashboard" as examples. If we ever rename those nodes, the docstring will quietly drift. Trivial; defer.

### Where the layering is clean

- `core/` and `node_sdk/` modules contain **no** imports of `nodes.*` or `dashboard.*`, and **no** string references to specific node IDs (per `notes/cleanup_pass.md`'s scan). The code itself is well-layered; the *notes* are where most of the leakage lives.
- `notes/security_hardening.md`'s three shipped fixes are all clean protocol-layer changes.
- `notes/cleanup_pass.md`'s removals and docstring additions are clean.

---

## 7. Appendix — every artifact

### Protocol-layer code touched tonight
- `core/core.py` — three security fixes (no default admin token, header-only, token-bucket rate-limit, bounded queues)
- `core/manifest_validator.py` — new file, 19 tests, not yet wired
- `core/supervisor.py` — docstrings only
- `node_sdk/__init__.py` — docstrings only
- `node_sdk/sse.py` — extracted `SSEHub`, docstrings, dead-import cleanup
- `nodes/kanban_node/kanban_node.py`, `nodes/nexus_agent/agent.py`, `nodes/nexus_agent_isolated/agent.py`, `nodes/voice_actor/audio_io.py`, `nodes/voice_actor/realtime_client.py` — dead-import cleanup; nexus_agent and kanban_node migrated to `node_sdk.sse.SSEHub`

### Notes — wave 1 (synthesis)
- `notes/PROTOCOL_CONSTRAINT.md` — the hard constraint. Read first.
- `notes/synthesis_20260510.md` — wave 1 synthesis. §6 contains the dashboard-as-node bold proposal.
- `notes/v1_prd_draft.md` — HR-1..HR-19, A1..A10 acceptance tests.
- `notes/migration_path.md` — 6-week Python→Elixir plan, 2026-05-11 → 2026-06-21.
- `notes/security_audit_20260510.md` — V-01..V-20.
- `notes/architecture_compare.md` — 4-prototype matrix.
- `notes/benchmark_results_20260510.md` — protocol round-trip / throughput / cold-boot.
- `notes/capability_graph/MODEL.md` — Datalog formalization, OCapN/seL4/Macaroons/Meadowcap mappings, four extensions.
- `notes/capability_graph/demo.py`, `notes/capability_graph/example_extended_manifest.yaml` — companion artifacts.
- `notes/operational_playbook.md` — six runbooks.
- `notes/testing_strategy.md` — 145 tests, 22s wall, 43% coverage; layered test plan.
- `notes/mesh_only_ideas_20260510.md` — 15 capability ideas; pick is #11+#2.
- `notes/inspiration_20260510.md` — "steal X" recommendations; layer-tag before acting.

### Notes — wave 2 (critique + implementation)
- `notes/wave_1_critique.md` — **read this first if read in full.** Brutal review; 10 layer-leak items.
- `notes/dashboard_node_v2.md` — refactor of synthesis §6; 6-PR migration plan; trim per R5.
- `notes/security_hardening.md` — three protocol fixes shipped.
- `notes/security_postmortem.md` — gap matrix; layered diffs; open questions feed Q1–Q3 above.
- `notes/sse_consolidation.md` — `SSEHub` extracted; `Last-Event-ID` semantics flagged broken.
- `notes/manifest_validation_design.md` — validator landed; two-stage rollout; `core` reserved id flagged.
- `notes/cleanup_pass.md` — non-functional cleanup; layering scan results.
- `notes/docs_audit.md` — README/PROTOCOL/PROTOTYPE audit; replacement drafts in `notes/docs_drafts/`.

### Experiments (not exercised tonight; preserved as the rewrite recipe)
- `experiments/elixir_mesh/` — 730 LOC, BEAM supervision, hot-reload candidate.
- `experiments/rust_mesh/` — 1361 LOC, 5.9MB binary, 23ms cold start. Reserved for nodes.
- `experiments/go_mesh/` — 1215 LOC. Backup if Elixir litmus tests don't trip.
- `experiments/nats_pivot/` — 511 LOC SDK. Use as transport-only, not as protocol replacement.
- `experiments/multi_host/`, `experiments/agent_process_model/`, `experiments/tool_discovery/`, `experiments/mesh_only_top1/`, `experiments/mesh_only_top2/` — preserved for the next wave.

### Tonight's deltas (one-line summary)
- 3 protocol fixes shipped (`core/core.py`).
- 1 new protocol module landed but not wired (`core/manifest_validator.py`).
- 1 SDK refactor partially complete (`node_sdk/sse.py`, 2/8 sites).
- 145 tests → 145 tests after cleanup; 125 → 125 after security hardening (different baselines).
- 0 node logic changes.
- 0 dashboard refactor PRs (still proposal-stage).
- ~17 notes documents produced or revised.

---

*End of briefing. Coffee should be done. Suggested reading order if you have more than 15 minutes: §1 → §6 → §3 → §4. The wave 1 critique (`notes/wave_1_critique.md`) is the single highest-value note tonight.*
