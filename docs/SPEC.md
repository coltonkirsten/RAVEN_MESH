# RAVEN Mesh — Specification

**Status:** authoritative. This document defines the protocol. Code follows
this document. When code and this document disagree, this document wins and
the code is wrong.

**Versioning:** wire prefix `/v0/`. Breaking changes bump the prefix. This
document covers `v0`.

---

## 1. Vocabulary

- **Node** — a participant in the mesh. Has a stable `id` and an HMAC shared
  secret.
- **Surface** — a typed, named entry point on a node. Identified by
  `{node_id}.{surface_name}`. Has a `type`, an `invocation_mode`, and a
  JSON Schema for its input payload.
- **Relationship** — a directed edge `(from_node, to_surface)`. Edge present
  ⇒ permitted. Edge absent ⇒ denied. There is no policy field, no priority,
  no role.
- **Manifest** — a YAML file declaring nodes, surfaces, and relationships.
  Loaded at Core startup; mutable at runtime through Core surfaces.
- **Core** — the broker. Verifies identity, enforces edges, validates
  schemas, routes envelopes, writes audit.
- **Envelope** — the unit of mesh traffic. Defined in §3.

## 2. Surface types and invocation modes

- `type`: `tool` | `inbox`
- `invocation_mode`: `request_response` | `fire_and_forget`

No other types or modes exist in `v0`.

## 3. Envelope

```json
{
  "id": "msg-uuid",
  "correlation_id": "trace-uuid (= id for the first message in a trace)",
  "from": "node_id",
  "to": "node_id.surface_name",
  "kind": "invocation" | "response" | "error",
  "payload": { ... schema-validated ... },
  "wrapped": { ...optional inner envelope when forwarded by an approval node... },
  "timestamp": "ISO-8601 UTC",
  "signature": "<hex hmac-sha256>"
}
```

Routing is by surface id. Envelopes never carry transport addresses.

### 3.1 Signing

```
canonical(env) = JSON of (env without "signature"),
                 sorted keys, no whitespace, separators = (",", ":")
signature      = hex( HMAC-SHA256( secret, canonical(env) ) )
```

Verification uses constant-time compare.

### 3.2 Registration body

`POST /v0/register` body is signed by the same rule as envelopes but is not
itself an envelope:

```json
{
  "node_id": "tasks",
  "timestamp": "ISO-8601 UTC",
  "signature": "<hex hmac-sha256>"
}
```

### 3.3 Replay protection

Core rejects any envelope or registration body whose `timestamp` lies outside
±`replay_window_seconds` of Core's clock. Default `60`. Bounded `[5, 300]`.

For `/v0/invoke` and `/v0/respond`, Core additionally rejects any envelope
whose `id` has already been seen inside the replay window (nonce LRU).

Registration replay is rejected on timestamp alone (no nonce field on
`/v0/register` until SDK envelope-id bump).

## 4. Channels

Three HTTP endpoints exist on Core:

### 4.1 `POST /v0/register`

Body: registration body (§3.2). Returns:

```json
{
  "session_id": "<uuid>",
  "node_id": "tasks",
  "kind": "<echo of manifest kind, or null>",
  "surfaces": [{ "name": "create", "type": "tool", "invocation_mode": "request_response" }, ...],
  "relationships": [{ "from": "tasks", "to": "..." }, { "from": "...", "to": "tasks.create" }, ...]
}
```

`kind` is whatever string the manifest declared for the node (or `null`
if absent). Core does not branch on it; see §8.

### 4.2 `POST /v0/invoke`

Body: full envelope, `kind = invocation`. Core verifies signature, checks the
edge `(env.from, env.to)`, validates `env.payload` against the target
surface's schema, then delivers to the target.

Responses:

- `200` with response envelope — `request_response` surface, response
  arrived within timeout.
- `202` `{"id": "<msg-id>", "status": "accepted"}` — `fire_and_forget`
  surface.
- `400` `denied_schema_invalid`
- `401` `bad_signature`
- `403` `denied_no_relationship`
- `404` `unknown_node` | `unknown_surface`
- `503` `denied_node_unreachable`
- `504` `timeout`

### 4.3 `POST /v0/respond`

Body: full envelope, `kind = response` | `error`. Core matches
`correlation_id` to a pending invocation. Returns `200`
`{"status": "accepted"}` or `4xx` if unmatched / from wrong target.

### 4.4 `GET /v0/stream?session=<session_id>`

Long-lived `text/event-stream`. Core pushes:

```
event: hello
data: {"node_id": "tasks", "session_id": "..."}

event: deliver
data: <envelope JSON>

: heartbeat
```

`:` lines are SSE comments and may be ignored.

Last-Event-ID resume is not supported. The `id:` SSE line MAY be absent
on Core-emitted events. Nodes that disconnect re-register on reconnect;
the register response carries current state. Events missed during a
disconnect are not recovered.

Invocations to a disconnected node fail synchronously with `503
denied_node_unreachable`. They are not queued for delivery on the
target's reconnect. RAVEN Mesh is a stream-delivery system, not a
message queue. Callers that need durability ship it at the application
layer (retry, idempotency keys, an explicit queue node).

### 4.5 Out-of-band endpoints (operator tooling)

These exist for human and external-monitoring use. They are not part of
mesh traffic, are not subject to allow-edges, and are not required by the
spec — a v0-compatible Core may omit them.

| Endpoint | Purpose |
| --- | --- |
| `GET /v0/healthz` | Liveness probe |
| `GET /v0/introspect` | Manifest snapshot |
| `GET /v0/admin/stream` | Raw SSE tap of every routed envelope |
| `GET /v0/admin/metrics` | Prometheus-format metrics scrape |

`/v0/admin/stream` and `/v0/admin/metrics` are gated by `ADMIN_TOKEN` bearer
auth. No other `/v0/admin/*` endpoints exist.

## 5. The `core` node

`core` is a reserved node id. The Core process exposes itself as a first-
class node so that mesh control flows through the same allow-edge mechanism
as every other interaction.

### 5.1 Properties

- `id`: `core` (reserved; manifest validator rejects any user node with
  this id).
- Identity secret: read from env var `MESH_CORE_SECRET`. Never declared in
  manifests or config files.
- Always present in every running mesh whether or not the manifest names
  it. Listed in `/v0/register` snapshots and `/v0/introspect` output.
- Not self-registered: Core does not call its own `/v0/register`.
- Envelopes to `core.*` surfaces are dispatched to in-process handlers
  rather than pushed to an SSE stream.
- Reachable only via allow-edges. No edge to a `core.*` surface ⇒ no
  caller can reach it. A manifest with zero edges into `core` is a valid
  fixed-topology mesh.

### 5.2 Surfaces

All surfaces are `type: tool`, `invocation_mode: request_response` unless
noted. Schemas live at `schemas/core/{surface}.json`.

| Surface | Purpose | Side-effect |
| --- | --- | --- |
| `core.state` | Return manifest, nodes, edges, recent envelope tail. | none |
| `core.processes` | Return supervisor process listing. | none |
| `core.metrics` | Return counters and gauges as JSON. | none |
| `core.audit_query` | Return audit entries matching a filter (see §5.3). | none |
| `core.set_manifest` | Accept a manifest YAML string, validate, persist, reload. | manifest replaced |
| `core.reload_manifest` | Re-read the manifest currently on disk. | manifest reloaded |
| `core.spawn` | Start a supervised process by node id. | process started |
| `core.stop` | Stop a supervised process by node id. | process stopped |
| `core.restart` | Restart a supervised process by node id. | process restart |
| `core.reconcile` | Reconcile running processes against the manifest. | processes started/stopped |
| `core.drain` | Drain in-flight invocations in preparation for shutdown. | new invocations rejected |

There are exactly eleven `core.*` surfaces. No surface allows synthesizing
envelopes claiming to originate from a different node (no `invoke_as`).

### 5.3 `core.audit_query` semantics

Input payload (all fields optional; conjunctive):

```json
{
  "since": "ISO-8601 UTC",
  "until": "ISO-8601 UTC",
  "from_node": "<node_id>",
  "to_surface": "<node_id.surface_name>",
  "decision": "<decision code>",
  "correlation_id": "<uuid>",
  "last_n": 100
}
```

Output: JSON array of audit entries, most recent first, capped at
`last_n` (default 100, max 1000).

### 5.4 Manifest reload semantics

When `core.set_manifest` or `core.reload_manifest` succeeds:

- Existing node sessions remain open if their node id is still present
  in the new manifest.
- Sessions for nodes removed from the manifest are closed.
- All in-flight invocations that no longer pass the new edge check are
  failed with `denied_no_relationship`.
- Core emits a `manifest_reloaded` SSE event to every still-connected
  node's `/v0/stream`.

## 6. Authorization

Every interaction with Core is authorized by the same rule:

1. HMAC signature verifies against the sender's identity secret.
2. Timestamp lies within the replay window; envelope id is not in the
   nonce LRU.
3. For invocations: an edge `(env.from, env.to)` exists in the active
   manifest.
4. The payload validates against the target surface's JSON Schema.

There is no admin token in the mesh path. `ADMIN_TOKEN` exists only for
the out-of-band endpoints in §4.5.

## 7. Audit log

Core writes one JSON object per line to `audit.log`. Each entry contains:

```
{ id, timestamp, type, from_node, to_surface, decision,
  correlation_id, details }
```

`type` is `invocation` or `response`. `decision` is one of:

```
routed
denied_no_relationship
denied_signature_invalid
denied_schema_invalid
denied_unknown_node
denied_unknown_surface
denied_node_unreachable
denied_replay
timeout
```

Operations on `core.*` surfaces are logged through this same path with no
special-casing.

## 8. Manifest format

```yaml
nodes:
  - id: <unique node id>          # must not be "core"
    runtime: <opaque descriptor>   # e.g. local-process, docker:img, external-http, human
    identity_secret: env:<ENV_VAR> # or a literal string (discouraged outside dev)
    metadata: { ... free-form ... }
    surfaces:
      - { name: <s>,
          type: tool | inbox,
          invocation_mode: request_response | fire_and_forget,
          schema: <relative path to JSON Schema file> }

relationships:
  - { from: <node_id>, to: <node_id.surface_name> }
```

Schema paths are resolved relative to the manifest file. `metadata` is
opaque to Core.

The legacy `kind` field (`actor | capability | approval | hybrid`) is
not part of the wire schema. Core does not branch on it, so the
validator does not gate on it either. Manifests are free to carry a
human-readable `kind` string (or any other tag) under `metadata`; Core
will echo a top-level `kind` in `/v0/register` and `/v0/introspect` if
the manifest declares one, but does not require or constrain it.

Manifest validator rejects:
- A node with `id: core`.
- A relationship whose `to` references an unknown node or unknown surface.
- A surface whose schema file is missing or invalid JSON Schema.
- Duplicate node ids.
- Duplicate surface names within a node.

## 9. Configuration

Core configuration is loaded with precedence (highest wins):
CLI flags → environment variables → TOML config file → built-in defaults.

The TOML schema is documented in `mesh.toml.example`. Secrets
(`ADMIN_TOKEN`, `MESH_CORE_SECRET`, identity secrets) are env-var only
and never read from TOML.

## 10. Conformance

A node implementation is `v0`-compatible if it can:

1. Register via `POST /v0/register` using §3.1 signing.
2. Open and consume `text/event-stream` from `/v0/stream`, dispatching
   `deliver` events to handlers.
3. Sign every outbound envelope with HMAC-SHA256 over canonical JSON.
4. Respond to an invocation by `POST`ing a response envelope with
   `correlation_id = invocation.id` to `/v0/respond`.
5. Optionally, send invocations via `POST /v0/invoke` and handle the
   sync `200` / `202` / `4xx` / `5xx` response codes.

A Core implementation is `v0`-compatible if it implements §4.1 — §4.4 with
the semantics in §3, §6, §7, §8, and exposes the `core` node from §5 with
all eleven surfaces.

§4.5 endpoints are optional for Core implementations.
