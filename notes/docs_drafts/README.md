# RAVEN Mesh

**RAVEN Mesh is a protocol** for a uniformly-modeled, statically-routed
network of nodes — humans, agents, tools, devices, runtimes — talking to
each other through a thin broker that owns identity, routing, and audit.

This repository contains two things, and **the line between them is the
most important architectural fact in the project**:

| Layer | What lives there | Substitutable? |
| --- | --- | --- |
| **Protocol layer** (the moat) | `core/`, `node_sdk/`, `schemas/manifest.json`, the wire spec in `docs/PROTOCOL.md` | If you fork this repo, throw away every node and the dashboard, and build a totally different product on the same wire — the protocol layer should still feel right. |
| **Opinionated layer** (one product on top) | `nodes/`, `dashboard/`, `manifests/*demo*.yaml`, surface-specific schemas, the `scripts/run_*.sh` wrappers | Replaceable. Every node, the React dashboard, every demo manifest — none of it is part of the contract. |

If a change to the codebase only makes sense because we happen to ship a
kanban node or a voice node today, that change belongs in the opinionated
layer. The constraint is documented in `notes/PROTOCOL_CONSTRAINT.md` and
applies to every commit.

> **Status (2026-05).** Protocol stable at `/v0/`. The Python Core in
> `core/core.py` is the conformance reference for `tests/test_protocol.py`.
> Several opinionated nodes ship in `nodes/`; they are example tenants of
> the protocol, not part of it.

---

## What's in this repo

```
core/                          PROTOCOL LAYER
  core.py                      ~1k-line single-process Python broker.
                               Identity, routing, schema validation, audit.
  supervisor.py                Optional in-Core process supervisor.
  manifest_validator.py        Strict manifest validation (errors + warnings).
node_sdk/                      PROTOCOL LAYER
  __init__.py                  Tiny Python client (MeshNode helper).
  sse.py                       SSE consumer helpers.
schemas/                       PROTOCOL LAYER
  manifest.json                JSON Schema for the manifest format itself.
schemas/                       OPINIONATED LAYER (per-surface schemas)
  task_create.json, …          One file per surface declared by today's nodes.

nodes/                         OPINIONATED LAYER
  dummy/                       Protocol-test stubs (actor/capability/approval/hybrid)
  approval_node/               Human-in-the-loop forwarder with web UI on :8803
  cron_node/                   Scheduled invocations
  webui_node/                  Browser display surface on :8801
  human_node/                  Operator dashboard on :8802
  kanban_node/                 Kanban board capability on :8805
  nexus_agent/                 Long-running Claude agent on :8804
  nexus_agent_isolated/        Same, sandboxed in Docker, on :8806
  voice_actor/                 OpenAI Realtime voice surface on :8807

dashboard/                     OPINIONATED LAYER
  src/                         Vite + React + Tailwind operator UI on :5180.
                               Speaks the admin namespace of the protocol.

manifests/                     OPINIONATED LAYER
  demo.yaml                    Minimum protocol-conformance wiring (used by tests).
  full_demo.yaml               All real nodes wired together.
  voice_actor_demo.yaml,       Other demonstration shapes.
  kanban_demo.yaml, …

scripts/                       OPINIONATED LAYER
  run_mesh.sh                  Generic: parses a manifest, starts each node.
  run_<node>.sh                Per-node convenience wrappers.
  _env.sh                      Deterministic dev secrets — source this first.

tests/                         MIXED
  test_protocol.py             PROTOCOL: language-agnostic conformance flows.
  test_envelope.py             PROTOCOL: HMAC + schema edge cases.
  test_manifest_validator.py   PROTOCOL: validator behavior.
  test_admin.py                PROTOCOL: admin namespace contract.
  test_supervisor*.py          PROTOCOL: process supervisor.
  test_<node>.py               OPINIONATED: per-node regression tests.
```

---

## Protocol-layer tour

The contract is in `docs/PROTOCOL.md`. The shortest possible summary:

- Every participant is a **node** with a stable id and an HMAC secret.
- A node exposes one or more **surfaces** (named typed entry points).
  A surface is `tool` or `inbox`, `request_response` or `fire_and_forget`,
  with a JSON Schema validating its payload.
- A directed **edge** `(from_node, to_surface)` exists in the manifest, or
  the call is denied. There is no policy field — presence is the whole ACL.
- Every message is a signed **envelope**. Envelopes never contain a host,
  port, or transport address: routing is by surface id; where a node lives
  is the broker's problem.
- The broker (Core) owns four things: identity verification, registry,
  routing, and audit. Nothing else. Memory, scheduling, approvals,
  presentation — all are themselves nodes.

**Adding a node in any language** is the substitution test for whether the
protocol leaks opinion: a node implementation passes if it can `POST
/v0/register`, consume `GET /v0/stream` (server-sent events), and `POST
/v0/respond` with HMAC-signed envelopes. That's the whole contract.
`tests/test_protocol.py::test_step_10_external_language_node` is a working
~30-line example using only Python's stdlib HTTP client — no SDK.

If you only care about the protocol, read `docs/PROTOCOL.md` and stop.

---

## Opinionated-layer tour

The opinionated layer ships in this repo as **one specific product built
on the protocol**. It exists to (a) prove the protocol works at non-toy
scale, (b) be useful for operators who want a working RAVEN deployment
out of the box, and (c) be a reference for what an opinionated tenant
looks like. None of it is required.

The flagship demo wires:

- **`approval_node`** — receives a forwarding request, decides (human or
  policy), forwards or denies.
- **`cron_node`** — schedules invocations.
- **`kanban_node`** — board capability with a browser UI; mesh tools
  drive the same mutator the UI does.
- **`webui_node`** — browser-side message display.
- **`human_node`** — operator dashboard for invoking any allowed surface
  from a form generated off the surface's JSON Schema.
- **`nexus_agent` / `nexus_agent_isolated`** — Claude harnesses with an
  MCP bridge to the mesh.
- **`voice_actor`** — OpenAI Realtime voice loop that introspects the
  manifest and exposes its outgoing edges as model-callable tools.

The **dashboard** at `:5180` (built with Vite/React/Tailwind) speaks the
protocol's admin namespace and gives operators: a live envelope tap,
manifest editor with reload, surface inspector with a generated form,
process supervisor view, UI-visibility controls.

Each opinionated node has its own README under `nodes/<node>/`.

---

## Quick start

```bash
git clone https://github.com/coltonkirsten/RAVEN_MESH.git
cd RAVEN_MESH

# Protocol-layer Python deps:
pip install aiohttp pydantic pyyaml jsonschema croniter structlog \
            pytest pytest-asyncio

# Opinionated-layer extras (only if you want to run those nodes):
pip install openai sounddevice numpy   # voice_actor
# Dashboard:
cd dashboard && npm install && cd ..
```

```bash
# Run the protocol conformance tests (no nodes, just the spec):
python3 -m pytest tests/test_protocol.py tests/test_envelope.py \
                  tests/test_manifest_validator.py tests/test_admin.py -v

# Run the full opinionated demo (ALL nodes, dashboards, audit log):
export ADMIN_TOKEN=$(openssl rand -hex 16)
source scripts/_env.sh
scripts/run_mesh.sh manifests/full_demo.yaml

# Stop:
scripts/run_mesh.sh stop
```

`run_mesh.sh` parses any manifest, starts Core, then starts each node it
has a `scripts/run_<node>.sh` for, and prints clickable UI URLs. The
`run_demo.sh` and `run_full_demo.sh` scripts are thin wrappers for
specific manifests.

When the full demo is up:

| URL | What it is | Layer |
| --- | --- | --- |
| `http://127.0.0.1:8000/v0/healthz` | Core liveness | PROTOCOL |
| `http://127.0.0.1:8000/v0/introspect` | Declared nodes, surfaces, edges, current connection state | PROTOCOL |
| `http://127.0.0.1:8000/v0/admin/state` | Full snapshot (admin-token-gated) | PROTOCOL |
| `http://localhost:5180` | React operator dashboard | OPINIONATED |
| `http://127.0.0.1:8801` | webui_node display | OPINIONATED |
| `http://127.0.0.1:8802` | human_node operator UI | OPINIONATED |
| `http://127.0.0.1:8803` | approval_node queue | OPINIONATED |
| `http://127.0.0.1:8804` | nexus_agent inspector | OPINIONATED |
| `http://127.0.0.1:8805` | kanban_node board | OPINIONATED |
| `http://127.0.0.1:8806` | nexus_agent_isolated inspector | OPINIONATED |
| `http://127.0.0.1:8807` | voice_actor inspector | OPINIONATED |

Try this opinionated-layer flow once it's up: open `:8801` and `:8802`,
pick `webui_node.show_message` in the human dashboard, send
`{"text":"hello"}` — the webui browser updates live.

---

## Operating it

These knobs are **protocol-layer**:

- `ADMIN_TOKEN` (required). Core refuses to start without one and refuses
  the legacy default `admin-dev-token`. Rotate as you would any secret.
- `MESH_ADMIN_RATE_LIMIT` / `MESH_ADMIN_RATE_BURST` — token-bucket
  limits on the admin namespace. Default 60/min with a burst of 20.
- `MESH_INVOKE_TIMEOUT` — per-invocation request/response timeout.
  Default 30 seconds.
- `MESH_SUPERVISOR=1` (or `--supervisor`) — let Core own node lifecycle
  via its in-Core process supervisor. With `--auto-reconcile`, Core
  spawns every manifest node at boot and restarts on crash.
- `AUDIT_LOG` — path to the audit-log file. One JSON object per line.

These knobs are **opinionated-layer**:

- `OPENAI_API_KEY` — required by `voice_actor` only.
- Per-node `*_SECRET` env vars — `_env.sh` provides deterministic dev
  defaults; production deployments must override.

---

## Tests

```bash
python3 -m pytest -v                         # everything
python3 -m pytest tests/test_protocol.py     # protocol conformance only
```

The conformance bar is **only** `tests/test_protocol.py` and the other
tests in the PROTOCOL row of the directory map above. The per-node tests
are opinionated-layer regressions and are not part of the contract: a
fresh Core implementation in another language only has to pass the
protocol-layer tests.

---

## Where to read next

- **Protocol contract** — `docs/PROTOCOL.md`. Read this before writing a
  node in another language.
- **Architecture (this repo's implementation)** — `docs/ARCHITECTURE.md`.
  How the protocol layer is split across `core/` and `node_sdk/`, how
  the opinionated layer slots on top, and the data flow at runtime.
- **Layer constraint** — `notes/PROTOCOL_CONSTRAINT.md`. The single hard
  rule applied to every architectural decision.
- **Per-node docs** — `nodes/*/README.md`. Each opinionated-layer node
  describes its own surfaces and configuration.
