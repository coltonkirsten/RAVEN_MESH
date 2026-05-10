# RAVEN Mesh — Architecture

This document describes how the code in this repository is laid out and
how it runs. It covers **both** layers — the protocol layer
(`core/`, `node_sdk/`, the wire spec) and the opinionated layer that
ships with it (`nodes/`, `dashboard/`, demo manifests).

The protocol is the contract; everything else is one specific tenant of
the protocol. The diagrams in this file deliberately separate the two
visually so you can tell at a glance which side of the line any given
component lives on.

If you only want the contract, read `docs/PROTOCOL.md` and stop.
If you only want to run the demo, read `README.md` and stop.
This document is for people who want to **change** the implementation —
either layer — without leaking opinion across the line.

---

## 1. Layered view

```
+================================================================+
|                       OPINIONATED LAYER                        |
|                (replaceable; not part of the contract)         |
|                                                                |
|  +---------+ +---------+ +---------+ +-------------+ +-------+ |
|  | webui   | | kanban  | | nexus   | | voice_actor | | human | |
|  | _node   | | _node   | | _agent  | | (OpenAI RT) | | _node | |
|  | (:8801) | | (:8805) | | (:8804) | | (:8807)     | |(:8802)| |
|  +----+----+ +----+----+ +----+----+ +-----+-------+ +---+---+ |
|       |           |           |             |             |    |
|  +----+----+ +----+----+ +----+--------+   |             |    |
|  | cron    | | approval| | nexus_agent  |  |             |    |
|  | _node   | | _node   | | _isolated    |  |             |    |
|  +----+----+ +----+----+ | (Docker:8806)|  |             |    |
|       |           |      +------+-------+  |             |    |
|       |           |             |          |             |    |
|       |           |   +---------+---+      |             |    |
|       |           |   | dashboard   |      |             |    |
|       |           |   | (React,     |      |             |    |
|       |           |   |  :5180)     |      |             |    |
|       |           |   +------+------+      |             |    |
|       |           |          |             |             |    |
|       v           v          v             v             v    |
|  - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -|
|              wire (envelopes; HMAC; SSE; manifest)             |
|  - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -|
|       ^           ^          ^             ^             ^    |
+=======|===========|==========|=============|=============|====+
        |           |          |             |             |
        v           v          v             v             v
+================================================================+
|                       PROTOCOL  LAYER                          |
|                (the moat; spec'd in docs/PROTOCOL.md)          |
|                                                                |
|   +-----------------------------------------------------+      |
|   |                       Core                          |      |
|   |   identity | registry | routing | audit | admin     |      |
|   |   (core/core.py)                                    |      |
|   |                                                     |      |
|   |   + optional: process supervisor                    |      |
|   |     (core/supervisor.py)                            |      |
|   |   + manifest validation                             |      |
|   |     (core/manifest_validator.py)                    |      |
|   +-----------------------------------------------------+      |
|                                                                |
|   +-----------------------------------------------------+      |
|   |                  node_sdk (Python)                  |      |
|   |   MeshNode helper:                                  |      |
|   |   register / sign / SSE consume / respond           |      |
|   |   (node_sdk/__init__.py, node_sdk/sse.py)           |      |
|   +-----------------------------------------------------+      |
|                                                                |
|   +-----------------------------------------------------+      |
|   |               schemas/manifest.json                 |      |
|   |    (canonical machine-readable manifest schema)     |      |
|   +-----------------------------------------------------+      |
+================================================================+
```

Read top-down: **opinionated nodes call into the wire; the wire is
implemented by the protocol-layer Core; the SDK is one Python helper
implementing the node side of the wire**. The SDK is protocol-layer
code because it implements only contract behavior (register, sign,
consume SSE, respond). It has no opinions about what a node does.

The `dashboard/` (React/Vite) is opinionated — it is *one* operator UI
that speaks the protocol's optional admin namespace. A different team
could build a CLI, a Slack bot, or a TUI with the same admin endpoints
and the protocol layer wouldn't notice.

---

## 2. Component-by-component

### 2.1 Protocol layer — `core/`

`core/core.py` is the single Core process. Roughly:

| Group | Responsibilities | Why protocol-layer |
| --- | --- | --- |
| `canonical / sign / verify` | HMAC-SHA256 helpers; identical canonicalization to the SDK so signatures match by construction. | Defined in §2.1 of PROTOCOL.md. |
| `CoreState` | In-memory registry: declared nodes, live connections, sessions, edges, pending request/response futures, envelope tail (for the admin tap), voluntary UI status, optional supervisor handle, raw manifest nodes for supervisor consumption. | The data model implements §1, §3, §6 of PROTOCOL.md. |
| Handlers (`/v0/register`, `/v0/invoke`, `/v0/respond`, `/v0/stream`, `/v0/healthz`, `/v0/introspect`) | Implement §3. | Required by the spec. |
| Admin handlers (`/v0/admin/...`) | Implement §9 of PROTOCOL.md plus optional supervisor controls. | Required only if admin namespace exposed. |
| `_admin_authed`, `_AdminRateLimiter` | Token check + token-bucket on `/v0/admin/*`. | Required by §9.1 and §9.4. |
| `make_app` / `amain` / `main` | aiohttp wiring, manifest load on boot, graceful shutdown. | Bootstrap; not part of the wire. |

`core/supervisor.py` is an **optional** in-Core process supervisor. When
enabled (`--supervisor` or `MESH_SUPERVISOR=1`), Core owns node
lifecycle: spawn, monitor, restart on crash, hot-add and hot-remove on
manifest reload. When disabled, lifecycle is the operator's job (see
`scripts/run_mesh.sh`).

The supervisor's contract is intentionally generic:

- A `ChildSpec` describes a node's process (cmd, env, cwd, log path,
  restart policy). It does **not** know what a node does (no kanban-
  shaped fields, no voice-shaped fields).
- Restart policies: `permanent` (always restart), `transient` (restart
  on abnormal exit), `temporary` (never restart), `on_demand` (lazy
  spawn on first work, idle reap after a configurable window).
- A `runner_resolver` callable maps `node_id` plus the manifest-node
  dict to a `ChildSpec`. The default resolver finds
  `scripts/run_<node_id>.sh`. Future resolvers could read a `runtime`
  field, build Docker images, talk to remote runners, etc., without
  changing the supervisor.
- `reconcile()` diffs the manifest's desired set against the running
  set and acts: spawn missing, stop extras, leave matching alone.

The supervisor is protocol-layer because its contract is not about what
nodes do, only about their process lifecycle. The exact restart
policies and the `metadata.supervisor.*` manifest fields are therefore
candidates for promotion into PROTOCOL.md once a second Core (e.g. a
BEAM port) implements them. They are documented here, not there, until
that second implementation exists.

`core/manifest_validator.py` is a **pure** validator: takes a parsed
manifest dict and the directory it loaded from, returns
`(errors, warnings)` — never raises. The rules are listed in
PROTOCOL.md §6.1; this file is the canonical implementation.

### 2.2 Protocol layer — `node_sdk/`

`node_sdk/__init__.py` is a thin asyncio client (`MeshNode`) that hides:

- HMAC signing (`canonical`/`sign` mirror Core).
- The two-phase startup (`connect()` registers; `serve()` opens the
  SSE stream).
- The SSE consumer + dispatcher loop (parsing `event:` / `data:` /
  comment lines, handling the `hello` and `deliver` event kinds).
- Handler-return semantics: `dict` → response envelope; `None` → no
  response (intended for fire_and_forget); `raise MeshDeny(reason,
  **details)` → `kind=error`; any other exception → `kind=error` with
  `reason="handler_exception"` (safety net).
- `invoke()` for outgoing calls (`wait=True` for request_response,
  `wait=False` for fire_and_forget, `wrapped=...` for forwarding).

`node_sdk/sse.py` is the bare-stdlib SSE helper used by some nodes that
don't want the full `MeshNode` wrapper.

The SDK is convenience, not contract. The contract is the wire; any
language can implement the wire without using or porting this SDK. The
test `tests/test_protocol.py::test_step_10_external_language_node`
proves this: a ~30-line external node speaks the protocol with hand-
rolled stdlib HTTP and no SDK.

### 2.3 Opinionated layer — `nodes/`

Each subdirectory is an example tenant of the protocol. They are
**replaceable**; the line above gives the substitution test. None of
them are required for protocol conformance. Brief sketches:

- **`dummy/`** — protocol-test stubs (one per node kind). Used by
  `tests/test_protocol.py` to drive each conformance flow.
- **`approval_node/`** — forwarder with a browser UI on `:8803`.
  Demonstrates §4.2 of the protocol.
- **`cron_node/`** — `request_response` tools to set/list/delete
  schedules; persists schedules to local disk.
- **`webui_node/`** — capability with a browser display on `:8801`.
  Live-updates via its own SSE channel.
- **`human_node/`** — operator dashboard on `:8802` with a form
  generated from the target surface's JSON Schema; calls the protocol's
  admin namespace to fetch schemas.
- **`kanban_node/`** — capability with a board UI on `:8805`. The same
  mutator backs both the browser UI and the mesh-tool surfaces.
- **`nexus_agent/`** — actor wrapping a Claude CLI process; bridges
  mesh tools into Claude via MCP. Inspector on `:8804`.
- **`nexus_agent_isolated/`** — sibling of `nexus_agent` but spawns
  Claude inside Docker with a sandboxed credential path. Inspector on
  `:8806`.
- **`voice_actor/`** — actor that opens an OpenAI Realtime WebSocket,
  captures the mic, plays the model's audio, and forwards transcripts
  into the mesh. Introspects the manifest at session start to expose
  outgoing edges as Realtime function-call tools. Inspector on `:8807`.

Two patterns recur and are worth naming:

- **Capability + inspector pattern.** Most opinionated nodes have a
  `MeshNode` subclass plus an aiohttp app on a dedicated port that
  serves a small inspector page over its own SSE channel. The pattern
  could be factored into a `node_sdk.WebInspector` helper; today it is
  reimplemented per node.
- **Mesh-tool injection.** `voice_actor` reads `/v0/admin/state` (or
  `/v0/introspect`) at session start and registers one Realtime
  function-call tool per outgoing edge. `nexus_agent` does the same
  via MCP. Any future agent harness can do the same with no protocol
  changes.

### 2.4 Opinionated layer — `dashboard/`

A Vite + React + Tailwind operator UI on `:5180`. Pages:

- **Live Logs** — connects to `/v0/admin/stream` for an envelope tap.
- **Mesh Builder** — edits the manifest YAML and posts to
  `/v0/admin/manifest`, then `/v0/admin/reload`.
- **Surface Inspector** — picks a surface and posts to
  `/v0/admin/invoke`.
- **UI Visibility** — toggles the voluntary `node_status` flags via
  `/v0/admin/node_status`.
- **Processes** — when the supervisor is enabled, talks to
  `/v0/admin/processes` and the lifecycle endpoints.

The dashboard depends only on `/v0/admin/*` plus `/v0/healthz` and
`/v0/introspect`. It does not import or require any specific node.
Replacing it with a CLI or a different UI is purely an opinionated-
layer change.

### 2.5 Manifests, scripts, and tests

`manifests/*.yaml` are opinionated wirings. `manifests/demo.yaml`
exists to drive `tests/test_protocol.py`; the others wire specific
demonstration tenants.

`scripts/run_mesh.sh` is the generic runner: it parses any manifest,
starts Core, then starts each node it has a `scripts/run_<node>.sh`
for, prints clickable UI URLs. `scripts/_env.sh` provides deterministic
dev secrets.

Tests fall into two camps:

- **Protocol conformance.** `tests/test_protocol.py`,
  `test_envelope.py`, `test_manifest_validator.py`, `test_admin.py`,
  `test_supervisor.py`, `test_supervisor_integration.py`. These exercise
  the protocol-layer code only and define the contract a fresh Core
  must pass.
- **Opinionated regression.** `test_kanban_node.py`,
  `test_nexus_agent*.py`, `test_voice_actor.py`, `test_mesh_db_node.py`.
  These exercise specific nodes; they are not part of the protocol
  bar.

---

## 3. Data flow at runtime

```
                          PROTOCOL LAYER
+----------------------------------------------------------------+
|                                                                |
|       +-----------------------+        manifest.yaml           |
|       |                       | <-----+---------------+        |
|       |        Core           |       |  load + validate        |
|       |                       |       |  (manifest_validator)   |
|       | nodes_decl, edges,    |                                 |
|       | sessions, pending,    |                                 |
|       | envelope_tail         |                                 |
|       +-----------------------+                                 |
|        ^   ^         ^   ^                                      |
|        |   |         |   |                                      |
|  POST  |   | SSE     |   | SSE                                  |
|/register|  |/v0/stream|  |/v0/admin/stream                      |
|  POST  |   | (deliver,|  | (envelope tap)                       |
|/v0/invoke| |  hello,  |  |                                      |
|  POST  |   |  close)  |  |                                      |
|/respond|   |          |  |                                      |
|        |   |          |  |                                      |
+========|===|==========|==|====================================+ |
|        |   |          |  |       OPINIONATED LAYER              |
|        v   v          v  v                                      |
|     +-----------+  +-------------+                              |
|     |  Node A   |  |  dashboard  |   (admin namespace ops only) |
|     | (any kind)|  |  (React)    |                              |
|     +-----+-----+  +-------------+                              |
|           |                                                     |
|  invoke   |   deliver                                           |
|  (sync)   v   (SSE)                                             |
|     +-----------+                                               |
|     |  Node B   |                                               |
|     | (target)  |                                               |
|     +-----+-----+                                               |
|           |                                                     |
|           v   POST /v0/respond  (resolves Node A's /invoke)     |
|     [back to Core ----------------------------> back to Node A] |
|                                                                 |
+-----------------------------------------------------------------+
```

### 3.1 Tool call (the workhorse path)

1. Both nodes **register**. Each gets a `session_id` and opens
   `GET /v0/stream?session=...`.
2. Node A signs an envelope `{from: A, to: B.tool, kind: invocation,
   payload}` and `POST /v0/invoke`s it. The HTTP call blocks (long-
   poll) up to `MESH_INVOKE_TIMEOUT` seconds.
3. Core verifies signature, checks the edge `(A, B.tool)`, validates
   `payload` against `B.tool`'s schema, audits `routed`, **enqueues** a
   `deliver` event onto Node B's SSE queue, registers a pending future
   keyed by the envelope's `id`.
4. Node B's SSE loop receives the event, runs the handler, signs a
   response envelope (`from: B`, `correlation_id: <A's id>`,
   `kind: response`, `payload`), and `POST /v0/respond`s.
5. Core validates the response (`from` must equal the original target),
   audits `routed`, sets the pending future's result.
6. Node A's blocked `/v0/invoke` HTTP call returns 200 with the
   response envelope.

Failure modes audited as discrete decisions: signature invalid → 401;
no edge → 403; schema invalid → 400; target unknown → 404; target not
connected → 503 `denied_node_unreachable`; target connected but its
delivery queue is full → 503 `denied_queue_full`; no response within
timeout → 504. See PROTOCOL.md §3.1 and §5.

### 3.2 Forwarding path

A forwarder receives an invocation, decides locally (UI prompt, policy,
LLM, rate-limit), and forwards by issuing a fresh `/v0/invoke` with
`from: <forwarder>`, `to: <inner_target>`, and the original envelope
preserved in `wrapped`. When the inner response arrives, the forwarder
`POST /v0/respond`s on the **original** correlation_id, resolving the
caller's blocked HTTP call. Approval gates are the canonical example;
audit taps and rate-limiters are other shapes.

### 3.3 Admin path

`POST /v0/admin/invoke` synthesizes a signed envelope on behalf of any
registered node, then enters the same routing pipeline as §3.1 with
`signature_pre_verified=True`. The audit-log entry is indistinguishable
from one produced by the named node directly — operators relying on
audit forensics must treat the admin token as a privilege equivalent to
every node's HMAC secret combined.

`GET /v0/admin/stream` taps every routed envelope and replays the most
recent N (default 200) on connect. The dashboard uses this for Live Logs.

---

## 4. The two-layer rule, applied

To avoid leaking opinion into the protocol, every change goes through
the substitution test from `notes/PROTOCOL_CONSTRAINT.md`:

> Could someone fork RAVEN_MESH, throw away every node and the
> dashboard, build a totally different product on the same protocol —
> and have the protocol still feel right?

Worked examples:

| Change | Layer | Reasoning |
| --- | --- | --- |
| Add a new restart strategy `on_demand` to the supervisor | PROTOCOL | Generic process-lifecycle behavior; any tenant could use it. |
| Hardcode "kanban_node always uses on_demand" in the supervisor | OPINIONATED — wrong place | Hardcode in `manifests/full_demo.yaml`'s metadata block, not in core/. |
| Document the envelope's `wrapped` field as "for approval nodes" | PROTOCOL drift | The field is generic; the prose should describe forwarders, with approval as an example. |
| Add a "Mesh Builder" page to the dashboard | OPINIONATED | The admin endpoints already exist; only one operator UI is changing. |
| Add a new `/v0/admin/X` endpoint that only makes sense for Claude agents | OPINIONATED, wrong place | If Claude-shaped, the endpoint belongs on the agent node, not on Core. |
| Add a per-edge rate limit | Tricky | If the policy is "all of `/v0/invoke`", it's protocol; if it's "limit human_node → nexus_agent specifically", build a forwarder. |

---

## 5. How to refactor away from the Python implementation

The protocol is the contract; the Python Core is the conformance
reference. A future Core in another language is conformant if:

1. It exposes `/v0/register`, `/v0/invoke`, `/v0/respond`, `/v0/stream`,
   `/v0/healthz`, `/v0/introspect` per `docs/PROTOCOL.md`.
2. `tests/test_protocol.py` (and the other PROTOCOL-row tests in §2.5
   above) pass against it, port-pointed at the new Core.
3. If it claims `/v0/admin/*`, the dashboard works against it
   unchanged.

What gets thrown away when (e.g.) a BEAM Core lands: `core/core.py`'s
in-memory registry, its asyncio queues, its audit-line writer. What
keeps running unchanged: `node_sdk/`, every node, every manifest, the
dashboard. They only know `core_url` and the wire.

Keep the Python supervisor's child-spec contract intact in any port; a
shared admin namespace and a shared manifest validator make the swap
mechanical.

---

## 6. Caveats and known sharp edges

These are implementation properties of *this* Python build, not of the
protocol:

- **HTTP-on-localhost only out of the box.** TLS/Tailscale is left to
  deployment. The protocol is unchanged either way.
- **Audit log writes are O(N) line appends with an asyncio lock.**
  Fine for prototype scale; aggregating into a node is a future
  refactor.
- **No SSE `Last-Event-ID` resume.** If a node's SSE drops mid-flight,
  in-flight deliveries are not replayed. Reserved for `v0.x`.
- **Disk-local data in opinionated nodes** (`cron_node/data/`,
  `kanban_node/data/`, `nexus_agent*/data/`). Move-host-and-lose-data
  risk; not the protocol's concern.
- **Restart strategy throttle has no global circuit breaker.** A
  pathological crash storm across many nodes can churn the supervisor.
- **Dashboard uses `EventSource` + a custom token-injecting fetch
  bridge.** Browsers don't let `EventSource` send custom headers;
  worth knowing if you re-implement the dashboard.

---

## 7. Pointers

- The single rule that governs every decision in this document:
  `notes/PROTOCOL_CONSTRAINT.md`.
- The contract this document implements: `docs/PROTOCOL.md`.
- The reader-friendly entry point: `README.md`.
- Per-node opinionated docs: `nodes/<node>/README.md`.
- The conformance test that makes the protocol real:
  `tests/test_protocol.py`.

When in doubt, separate. If a feature could plausibly belong in either
layer, push it down into the opinionated layer and keep the protocol
surface minimal.
