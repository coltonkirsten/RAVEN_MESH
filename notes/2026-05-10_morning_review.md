# 2026-05-10 Morning Review — Conversation Notes

Context: Colton woke up after the overnight sprint, wants to walk through the
mesh foundations one piece at a time and pressure-test the design. This file
captures the conversation as it unfolds so we can review at the end.

---

## §1 — High-level state recap (RAVEN delivered)

What was already there going into the night:
- Working protocol core (envelope schema, HMAC, manifest, allow-edges, /v0/admin/*)
- Python supervisor + node SDK
- 4 reference nodes (kanban, voice_actor, webui, dashboard)
- A web dashboard surface

What got done overnight (3 waves, 32 workers):
- Protocol hardening: HMAC replay window, Last-Event-ID monotonicity, SSE consolidation started
- Two clean-room prototypes (mesh_only_top1 + mesh_only_top2) — protocol-only, no nodes/dashboard
- Wave 1 critique, migration path doc, testing strategy, benchmark design, docs audit, cleanup pass
- Constraint propagated: protocol stays unopinionated, dashboard/nodes are the opinionated layer

State: nothing broken, all on main, 9 recommendations and 7 open questions queued for Colton.

---

## §2 — Plain English definitions

- **Envelope schema**: shape of every message. From, to, subject, timestamp, ID, body. Exact shape enforced.
- **HMAC**: tamper-proof seal. Sender + receiver share a secret. Sender stamps, receiver re-computes and compares. Detects tampering and proves origin.
- **Manifest**: a node's resume. Who I am, what I do, what I need. Published when node joins.
- **Allow-edges**: whitelist of who may message whom. Default-deny. Defined ONLY in the manifest yaml under `relationships:`.
- **/v0/admin/***: control-plane HTTP endpoints. Manage the mesh, not pass app messages.

---

## §3 — Allow-edges location confirmed

Allow-edges live ONLY in the manifest yaml, in the `relationships:` block.
Each entry: `from: node_id, to: node_id.surface`. Core reads at startup and on
/v0/admin/reload. Verified against demo.yaml. No other source of truth.

---

## §4 — HMAC vs alternatives

HMAC chosen because: symmetric, fast, simple, no certificate infrastructure.
Right tradeoff at our scale.

Alternatives:
1. mTLS — stronger, asymmetric, no shared secret. Needs CA, cert rotation. Overkill for personal mesh.
2. JWT with asymmetric signing — public-key verify, private-key sign. Better when verifiers shouldn't be able to forge. Not needed yet.

How K8s solves the same problem:
- Pod-to-control-plane: JWT (ServiceAccount tokens)
- Pod-to-pod: mTLS via service mesh (Istio/Linkerd, SPIFFE/SPIRE identities)
- Authz layer: RBAC
Three layers. We're collapsing into HMAC + allow-edges + manifest. Swappable later.

---

## §5 — Admin API endpoints (real list from core.py)

Always-on (Core):
- GET  /v0/admin/state        — full snapshot
- GET  /v0/admin/stream       — SSE tap of all envelopes
- POST /v0/admin/manifest     — write+validate new manifest yaml
- POST /v0/admin/reload       — re-read manifest from disk
- POST /v0/admin/invoke       — synthesize signed envelope from a chosen node
- POST /v0/admin/node_status  — node reports UI visibility state
- GET  /v0/admin/ui_state     — read all reported UI-bearing node states
- GET  /v0/admin/metrics      — metrics
- POST /v0/admin/drain        — graceful drain

Supervisor-attached only:
- GET  /v0/admin/processes    — list children (pid, status, uptime, restarts)
- POST /v0/admin/spawn        — start a node child process
- POST /v0/admin/stop         — stop a node child process
- POST /v0/admin/restart      — restart a node child process
- POST /v0/admin/reconcile    — diff manifest vs running, act

Why all 5 supervisor endpoints exist:
- processes = observability. You can't manage what you can't see.
- spawn/stop/restart = imperative escape hatches for one-off ops.
- reconcile = declarative bulk sync after manifest edits.
Same pattern as `kubectl apply` (reconcile) + `kubectl delete pod X` (imperative).

---

## §6 — Supervisor as a concept

Two layers of state in the mesh:
1. Declaration — manifest yaml. Which nodes exist, edges, secrets.
2. Processes — actual OS processes running each node.

Until last week, layer 2 was Colton's job (run_mesh.sh). Supervisor moves it
into Core. Editing manifest can now spawn/stop processes. Crashes auto-restart.

Mental model: like Erlang/OTP supervisor, systemd, or K8s' kubelet.

---

## §7 — Cross-machine crash/restart (today's reality + design space)

**Today**: Supervisor is local-only. Cross-machine MESSAGING works (HTTP +
HMAC + allow-edges over the wire). Cross-machine LIFECYCLE doesn't — Core
can't spawn or restart a node on a different machine.

Design options for going cross-machine:
1. Agent-per-machine (kubelet pattern) — tiny agent per box, supervisor talks to it.
2. SSH exec (Ansible pattern) — no agent, but harder to do persistent watch.
3. Self-supervising nodes — local supervisor per machine, federated via existing /v0/admin/*. Reuses protocol layer.
4. Containers — punt to Docker/k8s. Heavyweight, right for production.

Recommended path: Option 3 first (federated local supervisors), Option 4 if going production.

---

## §8 — DECISION POINT: dynamic vs supervised nodes (Colton's pushback, 11:48)

Colton's concern: committing to K8s-style supervised nodes loses the simplicity
of nodes managing themselves and being able to connect/disconnect ad-hoc. Real
example: voice_actor running as an app on his phone — when he closes the app,
how does the mesh think about that?

Two competing visions:
- **Supervised mesh**: Core/supervisor owns lifecycle. Predictable, observable, restarts on crash. K8s mental model.
- **Self-managing mesh**: nodes appear/disappear at will. Karpathy software-3.0 vibe — instructions are code, agents (like a coding-agent actor) interpret/install/spin up nodes on demand. Jarvis-like. RAVEN as a coding-actor that reconfigures the mesh.

Karpathy reference: software 3.0 = English instructions are the code, LLM is
the interpreter. Open-source projects with "paste this into Claude and it'll
install itself" READMEs are early-stage software 3.0.

Tension: supervised gives reliability and observability; self-managing gives
flexibility and dynamism. Neither alone is right.

**Conclusion (agreed 11:55):** support BOTH lifecycle styles. They're already
independent on two axes — lifecycle ownership (supervised vs self-managing)
and mesh dynamism (static vs dynamic). The protocol primitives we have today
(/v0/admin/manifest + reconcile + HMAC + manifest yaml) already enable both:

- **Persistent infrastructure nodes** → supervised. Predictable, observable,
  auto-restart. Kanban, dashboard, anything always-on.
- **Ephemeral / external nodes** → self-connected. Phone apps, laptop sessions,
  browser tabs. Drop in, present manifest fragment, leave when done. Supervisor
  never touched them.
- **Agent-spawned nodes** (Karpathy software-3.0 / Jarvis vision) → RAVEN as a
  coding-actor node calls /v0/admin/* to spin up/tear down nodes from English
  instructions. The "agent that reshapes the mesh" is itself an opinionated
  node on top of the unopinionated protocol — fork test passes.

Software 3.0 mapping: 1.0 = code humans write, 2.0 = NN weights, 3.0 = English
prompts AI interprets. Mesh-orchestrator-as-coding-actor = 3.0 applied to mesh
topology.

**Implication for protocol:** nothing to add. The plumbing is right. What's
missing is the agent-actor layer that USES the plumbing, and that's an
opinionated node, not protocol.

**Caveat to track:** the more dynamic the mesh, the more critical observability
becomes. /v0/admin/state and /v0/admin/stream are right; dashboard needs to
surface agent-driven mesh changes as first-class events, not bury them.

---

## §9 — DECISION POINT: ui-visibility and dashboard-as-node (Colton's pushback, 11:55)

Colton's concern: ui-visibility (`/v0/admin/node_status`, `/v0/admin/ui_state`)
and dashboard-as-node may be getting baked into the protocol when they should
be opinionated implementations on top.

**Findings (verified in code):**

UI-visibility lives in three places today:
1. Two admin endpoints in Core: `POST /v0/admin/node_status`, `GET /v0/admin/ui_state`
2. State on CoreState: `node_status: dict[str, dict]` (also dumped in /v0/admin/state)
3. Schemas: `schemas/ui_visibility.json` and `schemas/kanban_ui_visibility.json`

Dashboard-as-node check: grep'd "dashboard" in all of core/ — 4 hits, ALL in
comments, ZERO in code paths. Core has no `if node_id == "dashboard"`
anywhere. Dashboard is just another HTTP client of /v0/admin/*. Substitution
test passes — protocol could be forked + dashboard deleted, no protocol-layer
breakage.

**Verdict:**
- Dashboard-as-node: clean. No leak. Bundling is a packaging choice.
- UI-visibility endpoints + state field: real leak. Protocol layer has
  accidentally adopted "visibility" as a concept.

**Conclusion (agreed 12:03):** REMOVE visibility from Core entirely. Not
extracted to a node — just deleted. If a future mesh wants actor-node-driven
visibility state, that mesh will build it as opinionated nodes on top of the
clean protocol.

Removal scope:
- Delete `handle_admin_node_status` and `handle_admin_ui_state` from core.py
- Delete `state.node_status` field on CoreState
- Remove `node_status` from /v0/admin/state response payload
- Delete the two router registrations in core.py
- Update any callers (dashboard frontend, any node SDK helper, demo nodes)
  that POST /v0/admin/node_status today
- Keep `schemas/ui_visibility.json` and `schemas/kanban_ui_visibility.json` —
  these are surface schemas for nodes that opt in, not protocol concerns
- Delete the docstring lines in core.py that advertise these endpoints

This is R10 in priority queue (or jumps to R1.5 since it's small and
constraint-aligned).

Worker `2a3074e7` spawned 12:05 to execute removal on simplify-raven.

---

## §10 — Manifest validator (R1 from briefing)

**Background:** Manifest validation today is shallow — yaml parse + a few
required-field checks. Misses: relationship targets that don't exist, surface
references to missing schemas, broken JSON Schema, unresolved env-var secrets,
duplicate node ids, missing run scripts, schema/relationship type mismatches,
circular deps. Wave 2 worker built a real validator (schema-aware, structured
warnings + errors) — sitting on disk, not wired in.

**Three modes:**
1. Warnings-mode — runs on every load/reload, prints, doesn't block.
2. Strict-mode — errors block startup or reject /v0/admin/manifest POSTs.
3. Silent-mode — only via CLI tool.

**Conclusion (agreed 12:09):** wire validator in WARNINGS-MODE. Surface to
stdout/log only. Do NOT plumb warnings into /v0/admin/state response — that
would be the same kind of leak as visibility (Core inheriting an opinion about
what "warnings" mean to a UI). Dashboards can call a future explicit validate
endpoint if they want; for now stdout is the channel.

**Plan:**
- Wire validator on Core startup (after manifest yaml parse, before edge graph build)
- Wire on /v0/admin/reload (re-validate when manifest hot-reloads)
- Wire on /v0/admin/manifest POST (validate the incoming yaml before accepting)
- All three call sites print warnings to log/stdout, do NOT block
- Errors (vs warnings): even fatal-shaped issues (e.g. duplicate ids) print
  but don't block in warnings-mode. After a few weeks of audit, flip the
  switch to strict for the obvious-error class.
- No dashboard surfacing yet
- No new endpoint yet

---
