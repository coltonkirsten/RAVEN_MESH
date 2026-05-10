# Python → Elixir Migration Path

**Author:** migration-planning worker
**Date:** 2026-05-10
**Inputs:** `experiments/elixir_mesh/PORTING_ANALYSIS.md`, `notes/synthesis_20260510.md`, `notes/PROTOCOL_CONSTRAINT.md`
**Status:** plan, not commitment. The trigger conditions in `PORTING_ANALYSIS.md §5` still gate the actual go-decision.

---

## 1. Framing: this is a protocol-layer swap, not a rewrite

The single most important framing for this migration:

> **We are replacing the runtime that hosts the protocol. We are not replacing the protocol, and we are not replacing the nodes.**

The constraint document (`notes/PROTOCOL_CONSTRAINT.md`) draws a hard line between two layers. This migration touches only one of them:

| What changes | What does not change |
|---|---|
| `core/core.py` — Python aiohttp → Elixir Plug.Cowboy + GenServer | Envelope schema (from / to / surface / body / id / nonce / signature) |
| `core/supervisor.py` — hand-rolled supervisor → OTP DynamicSupervisor + `:transient` | HMAC signing rules and canonical-JSON byte format |
| Internal pending-future map → `GenServer.call` + small `pending: %{}` for HTTP responders | Manifest schema (nodes, edges, allow-edges, surfaces) |
| SSE plumbing → Phoenix.PubSub (or hand-rolled Plug SSE for HTTP boundary) | `/v0/register`, `/v0/invoke`, `/v0/respond`, `/v0/stream`, `/v0/healthz`, `/v0/introspect` URL contract |
| Connection table + session lifecycle → free, by virtue of process identity | `/v0/admin/*` URL contract (subject to §6 of synthesis: dashboard-as-node ideally lands first) |

Tagging:
- **[PROTOCOL]** — anything in the left column. The whole point of this migration is to swap the implementation while leaving the right column byte-for-byte stable.
- **[OPINIONATED]** — every existing node (kanban, voice_actor, nexus_agent(_isolated), human_node, webui_node, cron_node, approval_node, dummy nodes) and the React dashboard. **Untouched by this plan.** They keep speaking HTTP + HMAC to the new Elixir core and they don't notice the runtime changed.

If during execution we find ourselves modifying any node to "make the migration easier," that's the smoke alarm: the protocol contract has slipped, and we should fix the Elixir core to honor the existing contract, not bend the nodes.

---

## 2. The conformance contract is the migration's spine

`tests/test_protocol.py` is already the protocol-layer specification expressed as code. In particular `test_step_10_external_language_node` exercises a stdlib-only HTTP+HMAC+SSE node — no SDK — proving the protocol is portable.

**[PROTOCOL] decision for week 1:** the Elixir core ships when, and only when, the entire `tests/test_protocol.py` suite passes against it unmodified. The test runner shells out to a `mix run` server in a fixture, exactly as it does to a `python core/core.py` server today. No Elixir-specific test rewrite. If a test fails, the Elixir core is wrong; the test is the contract.

This is the same idea PORTING_ANALYSIS.md §4.5 surfaces: the external-language-node test is the spec. We promote it to gating status.

Secondary conformance asset to build in week 1: a **canonical-JSON cross-language golden vector** — a fixed list of envelopes whose canonical-JSON byte output is asserted byte-equal between `core/core.py` and `experiments/elixir_mesh/mesh/lib/mesh/crypto.ex`. Cost: 30 minutes (PORTING_ANALYSIS.md §5 already recommends this regardless of migration). Value: catches drift the moment it happens.

---

## 3. Which nodes go first?

**None.** That is the load-bearing claim of this plan.

The temptation when planning a Python→Elixir port is to draw up a list of nodes and rank them by porting difficulty. That instinct is wrong here, because **node migration is not on the critical path**. The protocol is the boundary. As long as the Elixir core implements the seven `/v0/*` endpoints identically, every Python node continues to register, sign, invoke, and stream without a line of Python changing.

The Elixir prototype already proves this is feasible: ~730 lines, 12 passing tests, supervised crash recovery, hot-add. What's missing is the HTTP shell. PORTING_ANALYSIS.md §3 estimates ~500–700 lines of Plug-based code to add it, mostly mechanical.

If we ever want to *write a new node in Elixir*, that's a Path B decision (PORTING_ANALYSIS.md §3.B), independent from this migration. **[OPINIONATED]** — when it happens it'll be one node at a time, evaluated on its own merits (e.g. cron is awkward in asyncio, natural in `:erlang.send_after/3`).

**[OPINIONATED] explicit non-goals during this migration:**
- Do not rewrite kanban_node, voice_actor, nexus_agent, or any other shipped node.
- Do not rewrite the React dashboard.
- Do not collapse `nexus_agent` and `nexus_agent_isolated` (synthesis §3 calls this out, but it's a separate refactor).

---

## 4. Hybrid operation: how Python and Elixir coexist

Because nodes don't migrate, "hybrid" is not a transition state — **it's the steady-state**. Python nodes talk to Elixir core forever, in the same way they talk to Python core today. There is no "everything is Elixir" end state implied by this plan; PORTING_ANALYSIS.md §3.C ("all-in rewrite") is explicitly out of scope.

**[PROTOCOL] hybrid mechanics:**

1. **The runtime is one process at a time.** Either the Python core is running on `:5170`, or the Elixir core is running on `:5170`. They are not run simultaneously against the same node set. The HTTP/HMAC/SSE contract is identical, so swapping is `kill PID; mix run`.
2. **Manifests don't change.** `manifests/full_demo.yaml`, `manifests/voice_actor_demo.yaml`, etc. are read by the Elixir manifest loader unmodified. (Pending: the Elixir loader should reject manifests with edges referencing undeclared nodes — synthesis §3 calls this out as a needed strict-mode improvement, and it's a perfect time to land it since both cores can adopt the same stricter check together.)
3. **`run_mesh.sh` works unchanged.** It parses the manifest and execs `run_<node>.sh` per node. The fact that core is now Elixir doesn't affect the bash convention. (When supervisor work matures, `run_mesh.sh` retires — but that's parallel work, not blocked on the migration.)
4. **Dashboard works unchanged**, *if* the synthesis-§6 dashboard-as-node refactor has not yet landed. The Elixir core implements `/v0/admin/*` identically. *If* dashboard-as-node has landed first (which I'd argue for — see §6 below), the migration gets simpler because there's less surface to port.

**[OPINIONATED] hybrid not in scope:**
- "Some nodes are Elixir, some are Python" *is* possible (Path B) but is a feature, not a transition step. If/when we add an Elixir node, it speaks the same HTTP protocol as a Python node would. There is no special "Elixir-native fast path" that bypasses the HTTP contract — that would be opinion leaking into the protocol.

---

## 5. Rollback story

Rollback is the easiest part of this plan, because the swap is symmetric.

**[PROTOCOL] rollback at any phase, in priority order:**

1. **Process-level rollback (seconds).** `pkill -f "mix run"`, `python core/core.py`. Both cores read the same manifest, the same env vars (`ADMIN_TOKEN`, `MESH_HOST`, `MESH_PORT`, plus per-node secrets), and produce byte-identical envelopes. Nodes will reconnect to the new core on their next register cycle. SSE consumers will see a brief disconnect and re-establish. This is the rollback for "Elixir core misbehaved, swap back, investigate at leisure."
2. **State rollback (none required).** The Elixir core, like the Python core, stores no durable cross-restart state on its own — declared nodes come from the manifest, sessions come from registration, edges come from the manifest. Node-side state (kanban's `data/board.json`, nexus_agent's `ledger/memory.md`) is owned by the nodes and survives core swaps. There is no migration of core-side state because there is no core-side state.
3. **Code rollback (git revert).** If a deployed Elixir core ships a behavioral regression that takes longer to fix than is tolerable, `git revert` of whichever launcher script changed (`run_mesh.sh`, the systemd unit, or the launchd plist that points at `mix run` instead of `python core/core.py`) and we're back. The Elixir core lives at `experiments/elixir_mesh/` until promoted; until promoted it cannot regress production.
4. **Long-term rollback contingency.** Keep `core/core.py` building and passing its tests for at least 60 days post-cutover. If we discover a corner case the Elixir core mishandles that's expensive to fix, we have a known-good fallback. After 60 days of clean operation, `core/core.py` becomes "reference implementation" — kept for the conformance test, not as a rollback target.

**The rollback property we're protecting:** at no point during the migration should there be a state that's reachable in Elixir but not Python (or vice versa). Anything that would create such a state — e.g. a new admin endpoint, a new envelope field, a new manifest key — should land in *both* implementations in the same week, or it shouldn't land.

---

## 6. Six-week milestone breakdown

Calendar: 2026-05-11 (Monday) → 2026-06-21 (Sunday). Each week ends on Sunday with a checkpoint. Hours assume single-developer evenings/weekends, ~10 hrs/week.

**Implicit prerequisite (not part of the 6 weeks):** the synthesis-§6 *dashboard-as-node* refactor lands in Python first. Without it, the Elixir migration also has to port the seven `/v0/admin/*` endpoints with full fidelity (manifest write, audit stream, etc.), which roughly doubles week 3. **[PROTOCOL] strong recommendation:** schedule dashboard-as-node before week 1 of this plan. If that's not on the table, add a week 0 between weeks 1 and 2 of the plan below for admin-endpoint porting.

### Week 1 — Conformance harness + canonical JSON parity
**[PROTOCOL]**
- Lift `tests/test_protocol.py` to run against an arbitrary `localhost:5170` server (parameterize the binary launched by the fixture).
- Land the canonical-JSON cross-language golden vector: a JSON file of N envelopes plus expected SHA256 of canonical bytes. Both `core/core.py` and `crypto.ex` assert the same hashes.
- All 19 protocol tests pass against the Python core unchanged. The Elixir core has not been touched yet.
**Checkpoint:** `pytest tests/test_protocol.py --core=python` and `pytest tests/test_protocol.py --core=elixir-prototype` both run. Elixir will fail most tests at this point — that's expected. Failures are the worklist.

### Week 2 — Elixir HTTP shell, register + invoke + respond
**[PROTOCOL]**
- Add `Plug.Cowboy` and Phoenix-less HTTP routing in `experiments/elixir_mesh/mesh/lib/mesh/http/`.
- Implement `POST /v0/register`, `POST /v0/invoke`, `POST /v0/respond` against the existing in-process core. The `pending: %{msg_id => from}` map (PORTING_ANALYSIS.md §4.2) lands here — ~20 lines.
- Verify HMAC signature verification against Python-signed envelopes byte-for-byte.
**Checkpoint:** the subset of `tests/test_protocol.py` that exercises register/invoke/respond passes against the Elixir core. SSE-dependent tests still fail.

### Week 3 — SSE delivery + admin endpoints
**[PROTOCOL]**
- Implement `GET /v0/stream` as Plug-streamed SSE per node. Bound the per-node mailbox at `maxsize=1024` (matching synthesis §4 recommendation; both cores get the same bound).
- Implement `/v0/admin/state`, `/v0/admin/stream`, `/v0/admin/manifest`, `/v0/admin/reload`, `/v0/admin/invoke`, `/v0/admin/node_status`, `/v0/admin/ui_state` against the existing in-process state. Use Phoenix.PubSub internally for the audit stream; the wire format stays SSE.
- If dashboard-as-node landed (recommended), this week is half the size and the audit stream is the canonical `core.audit_stream` surface, not an admin endpoint.
**Checkpoint:** all 19 `tests/test_protocol.py` tests pass against the Elixir core. The 12 prototype tests still pass. Total green: 31.

### Week 4 — Manifest loader strict mode + supervisor parity
**[PROTOCOL]**
- Port the strict manifest validator (`tests/test_manifest_validator.py`) — undeclared-node-edge rejection, duplicate-node-id detection, schema validation. Land it in *both* Python and Elixir, gated behind a `--strict-manifest` flag for one-week soak, then default-on.
- Port `tests/test_supervisor.py` and `tests/test_supervisor_integration.py` to run against the Elixir DynamicSupervisor. The contract surface (`/v0/admin/spawn`, `/v0/admin/restart`, etc., or whatever the dashboard-as-node lifecycle surfaces look like) must match.
**Checkpoint:** Python supervisor tests pass against Elixir supervisor. Strict manifest mode is on by default; existing `manifests/full_demo.yaml` has been fixed to declare `nexus_agent` (synthesis §3 issue), no other manifests broke.

### Week 5 — Soak under hybrid traffic
**[PROTOCOL] + [OPINIONATED] integration**
- Run the full real demo (`manifests/full_demo.yaml`) against the Elixir core for ≥72 hours. All Python nodes connected, real voice_actor sessions, real nexus_agent inbox traffic.
- Capture: SSE reconnect counts, mailbox high-water marks, latency p50/p95/p99 vs. the same workload on the Python core (`notes/benchmark_results_20260510.md` is the baseline).
- Required: no functional regressions (zero failed invocations that would have succeeded on Python core). Acceptable: latency within 1.5× of Python (we expect *better*; the bar is "not worse").
**Checkpoint:** soak report committed to `notes/elixir_soak_20260614.md`. Go/no-go decision for cutover.

### Week 6 — Cutover + 60-day rollback window
**[PROTOCOL]**
- Promote `experiments/elixir_mesh/` to `core_elixir/` (or replace `core/`, but keeping Python core building is the rollback insurance — see §5).
- Update `run_mesh.sh` to launch the Elixir core. The Python core stays buildable but unused.
- Document in README: "Core runtime is Elixir as of 2026-06-21. Python core retained for conformance testing."
- Open a calendar reminder for 2026-08-20: "delete `core/core.py` if Elixir core has been clean for 60 days, otherwise extend window."
**Checkpoint:** production mesh runs on Elixir core. Python core runs once daily in CI as the conformance peer. All Python nodes unchanged.

---

## 7. What this plan deliberately does not do

- **It does not port any node.** Path C is not in scope. The opinionated layer stays Python.
- **It does not introduce Elixir-specific protocol features.** No new envelope fields, no new admin endpoints, no Phoenix-only routes. If we discover Elixir makes a feature easy that Python doesn't, that feature is *protocol-layer optional* and lands in both cores.
- **It does not commit to multi-host distribution** (Erlang clustering, PORTING_ANALYSIS.md §5 trigger 3). That's a separate decision after the migration. The migration *makes that future decision feasible*, but doesn't pre-commit.
- **It does not replace the conformance test discipline.** `tests/test_protocol.py` remains the authoritative spec. Both cores pass it. New protocol features add new tests in the same suite.

The migration succeeds when an outside contributor can read `PROTOCOL.md`, write a 30-line Go node against it, and have that node work indistinguishably against either the Python or the Elixir core. That property is what "the protocol is the moat" means in practice — and protecting it is the only reason to do the migration this way instead of as a rewrite.
