# Mesh — Elixir/BEAM prototype of RAVEN_MESH core

A minimum viable BEAM port of the RAVEN_MESH core. It demonstrates
the parts of the protocol that are most natively shaped for OTP:
supervised concurrent processes with message passing.

## What's in here

```
lib/mesh/
  application.ex      Top supervisor: PubSub + Registry + NodeSupervisor + Core
  core.ex             GenServer: manifest, node registry, edge ACL, routing
  node_supervisor.ex  DynamicSupervisor for hot-add / hot-stop nodes
  node.ex             Behaviour + GenServer macro every node uses
  echo_node.ex        Demo: dummy echo node
  kanban_node.ex      Demo: in-memory kanban-ish node
  crypto.ex           HMAC-SHA256 wire-compatible with Python core
  manifest.ex         Loads JSON manifests (JSON instead of YAML — see Notes)
  tail.ex             Phoenix.PubSub broadcaster for envelope tail
manifests/demo.json   Tiny demo manifest used by bin/demo.exs
bin/demo.exs          End-to-end runnable demo
test/mesh_test.exs    12 tests covering crypto, routing, supervisor, tail
```

## Run it

```bash
cd mesh
mix deps.get
mix run bin/demo.exs    # boots manifest, invokes, hot-adds, kills, recovers
mix test                # 12 passing tests
```

The demo prints, in order:

1. Boot manifest and introspect
2. Successful invocations through Core (`voice_actor → kanban`)
3. Edge ACL denial for an undeclared relationship
4. **Hot-add** a new `echo2` node and immediately invoke it (Core never restarts)
5. **Crash recovery** — kill the kanban GenServer, watch the
   DynamicSupervisor restart it under the same registered name,
   verify routing recovers
6. PubSub tail — drain the envelopes Core broadcast during the run

## Wire compatibility with the Python core

`lib/mesh/crypto.ex` produces byte-for-byte identical canonical JSON
to the Python `core.canonical()` (sorted keys at every depth,
`signature` field stripped, no whitespace). This means:

- A Python node could in principle sign an envelope and have this
  Elixir core verify it (and vice versa).
- Envelope shape is identical: `{id, correlation_id, from, to, kind,
  payload, timestamp, signature}`.

What this prototype does **not** implement (intentionally — those
were not required to demonstrate the BEAM advantages):

- HTTP transport (`POST /v0/register`, `POST /v0/invoke`,
  `POST /v0/respond`, `GET /v0/stream`). Node↔Core messaging happens
  in-process via GenServer messages instead.
- JSON-Schema payload validation (the Python core uses
  `jsonschema.validate` per surface). The data flows through
  unchecked.
- Audit log (JSON-per-line). Nothing's persisted.
- Admin token gate / dashboard.
- YAML manifests (we use the structurally equivalent JSON shape).

These are all small additions if we promote the prototype: HTTP via
`plug_cowboy`, JSON-Schema via the `ex_json_schema` hex package,
audit via a logger backend or a separate `Mesh.Audit` GenServer
holding the file handle, YAML via `yamerl`.

## Test coverage

Run with `mix test`. Twelve tests covering:

- **crypto** — canonical JSON ordering / signature round-trip / tamper detection
- **routing** — request/response, fire-and-forget, edge ACL denial,
  unknown surface rejection
- **supervisor lifecycle** — hot-add, kill+restart, remove, handler
  exception isolation (the node stays alive when its handler raises)
- **tail** — every routed envelope reaches a PubSub subscriber

## Read this next

`PORTING_ANALYSIS.md` (one level up) — honest comparison to the
Python implementation: what gets cheaper, what gets harder, whether
the rewrite is worth doing now.
