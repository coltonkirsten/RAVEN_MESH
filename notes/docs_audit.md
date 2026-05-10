# RAVEN_MESH — documentation audit

**Author:** docs-audit worker
**Date:** 2026-05-10
**Scope:** `/README.md`, `/docs/PROTOCOL.md`, `/docs/PROTOTYPE.md`, plus what
documentation _should_ exist but doesn't.
**Lens:** the protocol-vs-opinionated layer constraint in
`notes/PROTOCOL_CONSTRAINT.md`.

Every finding below is tagged with the layer it belongs to:

- `[PROTOCOL]` — the unopinionated building block (envelope, signing, ACL,
  Core's broker contract, the admin namespace if/when we promote it).
- `[OPINIONATED]` — anything specific to today's nodes, today's dashboard,
  today's manifest content.
- `[META]` — documentation hygiene that doesn't favor either layer.

---

## Top-line: what's wrong with the docs today

The current docs do _not_ separate the protocol from the opinionated layer
that's been built on top of it. Today's `README.md` opens with an
"every participant is a node" pitch (good) but immediately leans into the
v0.4-era list of four reference nodes and never tells the reader that the
protocol is the moat, the dashboard is _one_ product, and any of these
nodes can be ripped out without touching the protocol. That framing is
the single most important thing to fix.

`docs/PROTOCOL.md` is in much better shape — it _is_ a protocol spec — but
it has accumulated drift: the admin namespace, the supervisor, manifest
validation, queue-full and admin-rate-limit error codes, and the fact that
the prototype has tripled in size are all undocumented or contradicted.

`docs/PROTOTYPE.md` is the most stale and is the right place for several
findings below to land — but it should probably be renamed
`ARCHITECTURE.md` and reframed around the protocol/opinionated split, not
"how to refactor away from Python." The "throw away when BEAM lands"
framing is a 2026-Q1 artifact that no longer matches the reality of
several thousand lines of working Python that the team is shipping
features on every day.

---

## A. README.md — findings

A1. `[META]` **The opening pitch never names the protocol/opinionated
split.** Line 3 leads with "every participant is a uniformly-modeled
node" — that's a protocol-layer claim — but lines 6–34 then list the
v0.4 reference nodes and dashboards as if they were the product. A
reader has no way to tell what they can throw out. **Fix:** lead with
two sentences: "RAVEN_MESH is a protocol. Everything in `nodes/` and
`dashboard/` is one opinionated product built on top." Then introduce
the rest.

A2. `[META]` **"v0 reference implementation: a single-process Python Core
(~430 lines) plus four real reference nodes" is wrong on every
number.** `core/core.py` is 1046 lines (2.4×); `core/supervisor.py` is
730 lines and unmentioned; `core/manifest_validator.py` is 209 lines
and unmentioned. There are not four real reference nodes — there are
seven (`approval_node`, `cron_node`, `human_node`, `kanban_node`,
`nexus_agent`, `nexus_agent_isolated`, `voice_actor`, `webui_node`),
plus four dummies. **Fix:** count the lines and nodes from the actual
tree at draft time, and stop pinning the headline to a number that
will keep drifting.

A3. `[META]` **"v0.4 — Python prototype, local-first" status block
implies the protocol is not yet a contract.** It is. The wire protocol
in `docs/PROTOCOL.md` has been frozen since v0.4 and `tests/test_protocol.py`
is the conformance test. The status block undersells this. **Fix:**
status should read "protocol is stable at /v0/; the Python prototype is
the conformance reference."

A4. `[META]` **`pip install` line is incomplete.** The README lists
`aiohttp pydantic pyyaml jsonschema croniter structlog pytest pytest-asyncio`.
Missing: `openai`, `sounddevice`, `numpy` (voice_actor); the dashboard
needs `npm`/`node`. **Fix:** split the install line: protocol-layer
deps (Core + SDK + tests) vs. node-specific deps (call out which
nodes need what).

A5. `[META]` **`python3 -m pytest` line says "all 19 tests pass."** There
are now 67+ tests across 12 test files (`test_admin.py`,
`test_envelope.py`, `test_kanban_node.py`, `test_manifest_validator.py`,
`test_mesh_db_node.py`, `test_nexus_agent_isolated.py`,
`test_nexus_agent.py`, `test_protocol.py`, `test_supervisor.py`,
`test_supervisor_integration.py`, `test_voice_actor.py`). **Fix:**
either drop the count or split it: "the 11 protocol-conformance flows
in `tests/test_protocol.py` (the contract) plus N node-specific tests
(opinionated-layer regression)."

A6. `[META]` **`scripts/run_demo.sh` and `scripts/run_full_demo.sh` are
both listed but `scripts/run_mesh.sh` — the generic, manifest-driven
runner — is not.** `run_mesh.sh` is the right entry point now;
`run_full_demo.sh` is a special case. **Fix:** lead with
`scripts/run_mesh.sh manifests/full_demo.yaml`, mention the others
as alternates.

A7. `[META]` **The dashboard is not mentioned in the README.** It runs
at `http://localhost:5180` (`scripts/run_mesh.sh:128`), is the
intended way to drive admin operations, and depends on the admin
token. A new contributor cloning the repo would not find it without
spelunking. **Fix:** add a "dashboard" section. Mark it explicitly
as `[OPINIONATED]` — it is one operator UI for the admin endpoints,
not part of the protocol.

A8. `[META]` **The dashboard table at lines 24–29 lists three node URLs
and Core's introspect/health endpoints, but doesn't list the
dashboard itself or the admin endpoints.** Same fix as A7.

A9. `[META]` **The repo tour (lines 38–56) lists `nodes/dummy/`,
`nodes/cron_node/`, `nodes/webui_node/`, `nodes/human_node/`,
`nodes/approval_node/` and stops there.** Missing: `kanban_node/`,
`nexus_agent/`, `nexus_agent_isolated/`, `voice_actor/`,
`ui_visibility.py`. Missing top-level: `dashboard/`, `experiments/`,
`notes/`. **Fix:** rewrite the tour from `ls`. Mark the protocol-layer
directories (`core/`, `node_sdk/`, `schemas/manifest.json`,
`schemas/`-as-spec) explicitly distinct from the opinionated layer
(`nodes/`, `dashboard/`, `manifests/*demo*`, node-specific schemas).

A10. `[META]` **"Spec" section points at `docs/PROTOCOL.md` and
`docs/PROTOTYPE.md`.** PROTOTYPE.md is misnamed — most of its content
is now load-bearing architecture, not "how to throw this away." **Fix:**
once the new `ARCHITECTURE.md` lands, redirect this section.

A11. `[META]` **PRD path at line 62 (`context/research/raven_mesh_v0_prd.md`)
is in the parent `raven` workspace and not in this repo.** Either
copy a sanitized version in or drop the reference. New contributors
won't have access to the parent workspace.

A12. `[META]` **No security or operations guidance whatsoever in
README.md.** No mention of `ADMIN_TOKEN` (which Core now refuses to
boot without — see `core/core.py:69`), no mention of secret
derivation in `_env.sh`, no mention of the audit log location, no
mention of supervisor/auto-reconcile. **Fix:** new "Operating it"
subsection. Tag each item with its layer (admin token →
`[PROTOCOL]`, supervisor restart strategies → `[PROTOCOL]`, the
specific list of nodes that have UIs → `[OPINIONATED]`).

A13. `[OPINIONATED]` **The "open http://127.0.0.1:8801" demo flow at
line 32 is one specific opinionated-layer demo.** It's fine to keep
in the README, but it must be labeled as a demo of one product
running on the protocol, not as how the protocol works. **Fix:**
move into a "Try the demo" subsection clearly distinct from the
"What is the protocol" section.

A14. `[META]` **"Adding a node in any language" at lines 73–79 is the
single best paragraph in the file** — it captures the protocol-layer
contract in three sentences. **Fix:** keep, promote it to immediately
after the protocol intro. It demonstrates the substitution test from
`PROTOCOL_CONSTRAINT.md` §"Validate by substitution" exactly.

---

## B. docs/PROTOCOL.md — findings

B1. `[PROTOCOL]` **The admin namespace `/v0/admin/*` is undocumented.**
Core exposes 14 admin endpoints (`/v0/admin/state`, `/admin/stream`,
`/admin/manifest`, `/admin/reload`, `/admin/invoke`,
`/admin/node_status`, `/admin/ui_state`, `/admin/processes`,
`/admin/spawn`, `/admin/stop`, `/admin/restart`, `/admin/reconcile`,
`/admin/drain`, `/admin/metrics`). PROTOCOL.md §3.3 stops at
`/v0/healthz` and `/v0/introspect`. **Decision needed:** are admin
endpoints part of the protocol contract or implementation-specific
add-ons? **Recommendation:** a thin slice (`/admin/state`,
`/admin/stream`, `/admin/reload`, `/admin/invoke`,
`/admin/node_status`, `/admin/ui_state`) is now load-bearing for any
operator UI and should be promoted to PROTOCOL.md as a separate
"§9 Admin namespace" with explicit token-auth semantics. The
supervisor endpoints (`/admin/processes`, `/admin/spawn`,
`/admin/stop`, `/admin/restart`, `/admin/reconcile`, `/admin/drain`,
`/admin/metrics`) are tied to today's process-supervision design and
should stay in `ARCHITECTURE.md` until a future spec bump.

B2. `[PROTOCOL]` **The error-code table in §3.1 is missing
`denied_queue_full` (HTTP 503).** Core can now reject an
otherwise-valid invocation when the target node's SSE delivery queue
is full (`core/core.py:332-337, 348-353`). This is observable to
external nodes; it must be in the spec. **Fix:** add it as a 503
sibling of `denied_node_unreachable` with a brief note that queue
sizing is implementation-defined but observable as a discrete error.

B3. `[PROTOCOL]` **The error-code table in §3.1 is missing `bad_kind`
(HTTP 400) and `bad_surface_id` (HTTP 400).** Both are emitted by
`_route_invocation` (`core/core.py:275-294`). The first happens when
a caller sends a non-invocation kind to `/v0/invoke`; the second
when `to` is missing the `node.surface` shape. **Fix:** add both.

B4. `[PROTOCOL]` **Audit decision codes at §5 do not list
`denied_queue_full`.** Audited at `core/core.py:332-337, 348-353`.
**Fix:** add to the list.

B5. `[PROTOCOL]` **Rate limiting on the admin namespace is not
documented.** Core implements a token-bucket rate limiter scoped to
`/v0/admin/*` (`core/core.py:797-877`), configured via
`MESH_ADMIN_RATE_LIMIT` and `MESH_ADMIN_RATE_BURST`, returning HTTP
429 with `{"error": "rate_limited", "scope": "admin"}`. If admin
endpoints become part of the spec (B1), so does this. **Fix:** add
under §3.3 or the new admin section, with an explicit note that
non-admin endpoints (`/v0/invoke`, `/v0/respond`) are _not_ rate
limited at the protocol level.

B6. `[PROTOCOL]` **Manifest validation is not specified.** The repo has
`schemas/manifest.json` and `core/manifest_validator.py` (209 lines)
that enforce: duplicate node IDs, reserved IDs (`core`), surface
name collisions, schema files exist and parse, edges reference
declared nodes/surfaces, env-resolved secrets warn when unset
(`core/manifest_validator.py:43-209`). **Fix:** add a §6.x
"Manifest validation" subsection that names the rules and points at
`schemas/manifest.json` as the canonical machine-readable spec.

B7. `[PROTOCOL]` **§6 says relative schema paths are resolved relative
to the manifest file. True. Worth keeping.** But §6 doesn't say
what happens when a schema file is missing or malformed — the
validator now produces error strings (`core/manifest_validator.py:144-158`)
and `core.load_manifest` will raise. **Fix:** a one-liner saying
"missing/unparseable schemas are a manifest error and Core refuses
to load."

B8. `[PROTOCOL]` **§2 envelope schema does not say `kind` is required
when posting to `/v0/invoke`.** It is, by `_route_invocation`
(`core/core.py:273-276`): `kind` must be `invocation` (or absent,
defaulted). Same constraint applies to `/v0/respond`: `kind` must be
`response` or `error`. **Fix:** make `kind` non-optional in the
envelope JSON shape, and tighten §3.1 / §4 with the per-endpoint
allowed values.

B9. `[PROTOCOL]` **§2 says "the envelope never contains a host, port,
or transport address." True for envelopes; subtle for registration.**
The registration body (§2.2) is also transport-free. Worth a one-line
restating to remove ambiguity. **Fix:** add to §2.2.

B10. `[PROTOCOL]` **The `wrapped` field in §2 says "an inner envelope
when forwarded by an approval node."** This is technically correct
but it bakes in the approval-node use case. The `wrapped` field is
generic — it lets _any_ forwarder preserve the chain. The example
should be approval-flow but the prose should not imply the field is
approval-specific. **Fix:** phrase as "an inner envelope when this
envelope is itself a forwarding of an earlier one (e.g. by an
approval-shaped node)." Substitution test: a future "rate-limit
node" or "audit-tap node" could use the same field.

B11. `[PROTOCOL]` **§4.2 (approval flow) is the single place in
PROTOCOL.md that names a specific opinionated-layer node ("approval
node B").** Per `PROTOCOL_CONSTRAINT.md` this is leaking opinion into
the protocol. **Fix:** rewrite §4.2 as "Forwarding flow" — describe
the shape of the protocol move (forwarder receives, signs a new
envelope with `wrapped`, responds with the inner result), and say
"approval is one use case." Move the explicit approval narrative to
the opinionated layer (the approval_node README or ARCHITECTURE.md).

B12. `[PROTOCOL]` **§4.2 says "Approval node's own decision logs are
local to that node — they are NOT part of Core's audit." True and
worth keeping, but rephrase per B11 as "a forwarder's internal
decisions are local; only Core's routing decisions land in
audit.log."**

B13. `[PROTOCOL]` **§3.1 `POST /v0/register` returns `{"kind": "capability"}`
in the example.** `kind` here is the node's declared kind (actor /
capability / approval / hybrid) — the same field used in the manifest
— and is distinct from the envelope's `kind`. Two different `kind`
fields meaning two different things in the same spec is a footgun.
**Fix:** rename one of them in prose ("node kind" vs. "envelope
kind"), or leave the wire format alone (it's deployed) and add a
§1 vocabulary note flagging the overload.

B14. `[PROTOCOL]` **§3.2 says "v0 nodes simply re-register on
reconnect."** Still true. But Core now closes the previous SSE
queue with a `_close` sentinel when the same node re-registers
(`core/core.py:229-236`). External implementers may want to know
the previous stream will be terminated for them. **Fix:** add
one line to §3.2.

B15. `[PROTOCOL]` **§7 conformance bullets do not mention queue-full
or rate-limit handling.** Conformance is "ten flows pass." If we add
B2 / B5, we should add the corresponding row: a v0-compatible node
must surface 503/429 to its caller without crashing. **Fix:**
extend §7.

B16. `[META]` **`pip install` example in PROTOTYPE.md is duplicated in
PROTOCOL.md style** — actually no, PROTOCOL.md is clean. (Noting so
the README/PROTOTYPE don't drift apart — see C-block.)

B17. `[PROTOCOL]` **§8 versioning paragraph says additive changes keep
`/v0/`.** B2/B3 (new error codes) and B5 (rate limiting) are
arguably _additive_ to the contract: existing well-behaved callers
won't see the new codes during normal operation. The doc should say
that explicitly so future contributors know which side of the line
new error codes fall on. **Fix:** one sentence in §8.

---

## C. docs/PROTOTYPE.md — findings

C1. `[META]` **The file is misnamed.** "PROTOTYPE.md, how this Python
implementation is structured + how to refactor away" worked when the
intent was to throw the Python Core out. The team is now adding
several thousand lines of Python that the protocol _doesn't_ require
(supervisor, dashboard, manifest validator, multiple production
nodes). The file's responsibility should split:
- The protocol-layer prose ("Core internals", "How nodes talk to
  Core via the SDK") goes into `ARCHITECTURE.md`.
- The opinionated-layer prose ("the dashboards are at 8801/2/3",
  "scripts/run_*.sh wrappers") goes into `ARCHITECTURE.md` under a
  separate "opinionated layer" section.
- "How to refactor away from Python" is now a long-tail concern;
  keep one short paragraph in `ARCHITECTURE.md`.
**Fix:** retire `PROTOTYPE.md` after `ARCHITECTURE.md` ships;
leave a stub redirect.

C2. `[META]` **"`core/core.py` is one file with a clear shape … ~430
lines"** is wrong (1046 lines, supervisor in a separate 730-line
file). **Fix:** rewrite the layout section from the actual tree.

C3. `[META]` **"three nodes" is wrong, and the dashboards-table omits
the React dashboard at :5180, the kanban_node UI (:8805), the
nexus_agent inspector (:8804), the nexus_agent_isolated inspector
(:8806), and the voice_actor inspector (:8807).** **Fix:** rebuild
from `scripts/run_mesh.sh:30-38`.

C4. `[META]` **"Core itself exposes" table omits `/v0/admin/*`.** Same
finding as B1, doc-side. **Fix:** include with a note that they're
admin-token-gated.

C5. `[PROTOCOL]` **"Core internals" lists `CoreState` fields including
`pending`, `connections`, `sessions`, `edges`** — these are the
right fields, but the list is now incomplete: `_admin_streams`,
`envelope_tail`, `node_status`, `supervisor`, `manifest_nodes_raw`
all live on `CoreState` (`core/core.py:117-127`). **Fix:** add or
explicitly say "the rest are admin-namespace bookkeeping."

C6. `[META]` **"How to refactor away from this Python implementation"
section is intact but the conformance bar is now stronger** — there
are 67+ tests across 12 files, not just `test_protocol.py`. The
protocol-conformance bar is _still_ `tests/test_protocol.py`; the
node-specific tests are not part of the contract. **Fix:** make
that distinction explicit. ARCHITECTURE.md should say "any new Core
implementation passes `tests/test_protocol.py`. The other test
files (test_kanban_node, test_voice_actor, etc.) are
opinionated-layer regression tests and only need to pass if you
keep the same nodes."

C7. `[META]` **"Known caveats" lists "no reconnect logic" and a few
others, but is missing several caveats that have surfaced since:**
- `nodes/cron_node/data/crons.json`, `nodes/kanban_node/data/board.json`,
  `nodes/nexus_agent/data/`, `nodes/nexus_agent_isolated/data/` are
  all opinionated-layer disk state. Move-host-and-lose-data risk.
  `[OPINIONATED]`
- The supervisor's restart throttle bounds runaway crash loops but
  has no global circuit breaker. `[PROTOCOL]` (it's in core/)
- Audit log writes are O(N) appends — already in the doc, keep.
- No `Last-Event-ID` resume — already in doc as v0.x reserve, keep.

C8. `[META]` **The "SDK contract" section is correct.** No drift here.
Keep as-is, move into ARCHITECTURE.md "node-side" section.

---

## D. Things missing entirely (no docs at all)

D1. `[META]` **No `ARCHITECTURE.md`.** That's what we're producing.

D2. `[PROTOCOL]` **No documentation of the supervisor's restart
strategies (`permanent`, `transient`, `temporary`, `on_demand`)** —
these are configurable per-node in the manifest's
`metadata.supervisor.restart` field (`core/supervisor.py:86-101`),
but the manifest schema (`schemas/manifest.json`) and the manifest
section of PROTOCOL.md do not document them. **Recommendation:** put
this in ARCHITECTURE.md as a `[PROTOCOL]`-layer feature of the
prototype Core (since the contract — restart on crash, with
configurable policy — is generic) but NOT in PROTOCOL.md until at
least one alt-language Core supports them.

D3. `[META]` **No `CONTRIBUTING.md` or operator runbook.** Several
things would benefit operators: where logs land (`.logs/`), where
PIDs land (`.pids/`), where the audit log lands (`audit.log`), how
to rotate `ADMIN_TOKEN`, how to run a single test file. Defer this
to a future doc; mention in ARCHITECTURE.md sidebar.

D4. `[OPINIONATED]` **No per-node README index.** Each `nodes/*/`
has a README — those are good — but there's no top-level pointer
to them. **Recommendation:** README.md "Node catalogue" subsection
that lists each opinionated-layer node with one-line summary +
dashboard URL + link to its README.

D5. `[META]` **`schemas/manifest.json` is the canonical manifest
schema and is unmentioned in any doc.** The ARCHITECTURE.md should
reference it.

D6. `[META]` **The dashboard's pages (`LiveLogs`, `MeshBuilder`,
`SurfaceInspector`, `UiVisibility`, `Processes`) are undocumented.**
This is opinionated-layer documentation; can wait. Note in the
ARCHITECTURE.md opinionated-layer section that the dashboard exists
and what it does, without listing every page.

D7. `[META]` **`notes/PROTOCOL_CONSTRAINT.md` itself is not linked from
any user-facing doc.** It's the most important architectural
document in the repo. **Fix:** link it from both README.md and
ARCHITECTURE.md (with the caveat that it's a constraint document,
not a user guide).

---

## E. Cross-cutting drift summary

The single recurring failure mode is that **the protocol layer (which has
been mostly stable since v0.4) and the opinionated layer (which has been
expanding daily) are not visibly distinct in the docs.** A reader cannot
tell which lines they have to honor to write a compatible Core in another
language vs. which lines describe one specific dashboard or node. Every
finding in A, B, and C is downstream of that single failure.

The drafts that follow this audit (`README_draft.md`, `PROTOCOL_draft.md`,
`ARCHITECTURE_draft.md`) split along that line:

- **README** introduces the split, points at both layers, lets the reader
  pick which they care about.
- **PROTOCOL** is _only_ the protocol — no node names, no dashboard
  references, no specific node URLs. The substitution test from
  `PROTOCOL_CONSTRAINT.md` §"Validate by substitution" applies: a fork
  that throws away `nodes/` and `dashboard/` should still feel right
  reading PROTOCOL.md.
- **ARCHITECTURE** has both halves with an ASCII diagram showing the
  boundary.

---

## F. What the drafts deliberately do NOT change

- **Wire format.** No envelope-shape changes. Sign rule unchanged. Edges
  unchanged. Surface types/modes unchanged.
- **Endpoint paths.** No new endpoints proposed. Documenting existing
  ones (admin, rate limit, queue-full) only.
- **Status semantics.** The protocol stays at `/v0/`. None of the
  documentation gaps are breaking.
- **Node-layer behavior.** No node implementation gets renamed or
  retired. No manifest gets edited. The `full_demo.yaml` discrepancies
  flagged in `synthesis_20260510.md` §3 are out of scope for a docs
  pass.

---

## G. Recommendation priority

1. Land `README.md` rewrite (audit findings A1, A2, A6, A7, A9, A12 are
   most-frequently-misleading).
2. Land `PROTOCOL.md` updates B1 (admin promotion), B2/B3/B4
   (error-code completeness), B11 (de-name approval node).
3. Land `ARCHITECTURE.md` as the home for everything in C plus D2/D5.
4. After all three land, retire `PROTOTYPE.md`.
