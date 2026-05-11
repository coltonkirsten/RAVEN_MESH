# RAVEN Mesh Wire Protocol — v0

> **Historical reference.** The authoritative wire-protocol specification is
> now [`docs/SPEC.md`](./SPEC.md). When this document and `SPEC.md` disagree,
> `SPEC.md` wins. This file is preserved for historical context (e.g.
> background on the v0 design discussions and the original conformance
> framing). New work should cite `SPEC.md`.

This is the language-agnostic specification. Anyone reading this should be able to write a node in any language without reading Core's source.

## 1. Vocabulary

- **Node** — any participant in the mesh. Has a stable id and a HMAC shared secret.
- **Surface** — a typed, named entry point on a node. `id = {node_id}.{surface_name}`.
  - `type`: `tool` (request/response) or `inbox` (typically fire-and-forget, but may be marked request/response — used by approval nodes).
  - `invocation_mode`: `request_response` or `fire_and_forget`.
  - `schema`: a JSON Schema validating the input payload.
- **Relationship** — a directed edge `(from_node, to_surface)`. Edge exists ⇒ allowed; no edge ⇒ denied. There is no policy field.
- **Core** — the single broker that owns identity verification, registry, routing, schema validation, and audit. Nothing else.
- **Manifest** — a YAML file declaring nodes, surfaces, and relationships. Loaded once at Core startup.

## 2. Envelope

Every message Core handles is a JSON envelope:

```json
{
  "id": "msg-uuid",
  "correlation_id": "trace-uuid (=id for the first message in a trace)",
  "from": "node_id",
  "to": "node_id.surface_name",
  "kind": "invocation" | "response" | "error",
  "payload": { ... schema-validated ... },
  "wrapped": { ...optional, an inner envelope when forwarded by an approval node... },
  "timestamp": "ISO-8601",
  "signature": "<hex hmac-sha256>"
}
```

The envelope **never contains a host, port, or transport address**. Routing is by surface id; where a node lives is Core's problem.

### 2.1 Signing

The signature covers a canonical JSON serialization of every envelope field **except `signature`**:

```
canonical(env)  = JSON of (env without "signature"),
                  with sorted keys, no extra whitespace, separators = (",", ":")
signature       = hex( HMAC-SHA256( secret, canonical(env) ) )
```

Concrete reference (Python; trivially portable):

```python
import hmac, hashlib, json
def canonical(env: dict) -> str:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))
def sign(env, secret: str) -> str:
    return hmac.new(secret.encode(), canonical(env).encode(), hashlib.sha256).hexdigest()
```

Verification computes the expected signature with the sender's known secret and compares with `hmac.compare_digest`-style constant time.

### 2.2 Registration body

The registration POST is **not** an envelope, but uses the same signing rule (whole body excluding `signature`):

```json
{
  "node_id": "tasks",
  "timestamp": "2026-05-09T18:00:00Z",
  "signature": "<hex hmac-sha256>"
}
```

## 3. Channels

Two HTTPS (or HTTP-on-localhost in v0 prototype) channels per node, both terminated at Core:

### 3.1 Outbound — Node → Core

| Endpoint           | Body                          | Purpose |
| ------------------ | ----------------------------- | ------- |
| `POST /v0/register` | registration body (above)    | declare presence; obtain a session id |
| `POST /v0/invoke`   | full envelope, kind=invocation | route to a target surface |
| `POST /v0/respond`  | full envelope, kind=response\|error | resolve a previously-received invocation |

`POST /v0/register` returns:

```json
{
  "session_id": "<uuid>",
  "node_id": "tasks",
  "kind": "capability",
  "surfaces": [{ "name": "create", "type": "tool", "invocation_mode": "request_response" }, ...],
  "relationships": [{ "from": "tasks", "to": "..."}, { "from": "...", "to": "tasks.create" }, ...]
}
```

`POST /v0/invoke` returns one of:

- `200` with the **response envelope** (when the surface is `request_response` and the response arrives in time).
- `202` with `{"id": "<msg-id>", "status": "accepted"}` (for `fire_and_forget` surfaces).
- `400` `denied_schema_invalid` — payload failed schema validation.
- `401` `bad_signature` — HMAC did not verify.
- `403` `denied_no_relationship` — no `(from, to)` edge in the manifest.
- `404` `unknown_node` / `unknown_surface`.
- `503` `denied_node_unreachable` — target node is not currently connected.
- `504` `timeout` — target did not respond within the invocation timeout.

`POST /v0/respond` returns `200` `{"status": "accepted"}` or `4xx` if the response cannot be matched to a pending invocation (or comes from someone other than the original target).

### 3.2 Inbound — Core → Node

`GET /v0/stream?session=<session_id>` — long-lived `text/event-stream`. Core pushes events in this format:

```
event: hello
data: {"node_id": "tasks", "session_id": "..."}

event: deliver
data: {"id":"...", "from":"voice_actor", "to":"tasks.list", "kind":"invocation", ...}

: heartbeat
```

The `: ...` lines (SSE comments) are heartbeats and may be ignored. Reconnect with `Last-Event-ID` is reserved for v0.x; v0 nodes simply re-register on reconnect.

### 3.3 Read-only (debugging)

| Endpoint            | Returns |
| ------------------- | ------- |
| `GET /v0/healthz`     | `{"ok": true, "nodes_connected": N, ...}` |
| `GET /v0/introspect`  | full manifest snapshot — declared nodes, edges, connection state |

## 4. The two flows you must implement to pass conformance

### 4.1 Tool call

1. Node A registers via `POST /v0/register`, opens its SSE stream.
2. Node A signs and `POST /v0/invoke`s an envelope `{from: A, to: B.toolX, kind: invocation, payload: {...}}`.
3. Core verifies signature, checks edge `(A, B.toolX)`, validates payload against `B.toolX`'s schema, then **pushes** a `deliver` event into B's SSE stream containing the same envelope.
4. Node B reads the deliver event, runs its handler, builds a response envelope `{from: B, to: A, correlation_id: <A's invocation id>, kind: response, payload: {...}}`, signs it, and `POST /v0/respond`s it.
5. Core matches `correlation_id` to A's pending invocation, returns the response body to A's open `/v0/invoke` HTTP call.

### 4.2 Approval flow

The approval node is just a node. Concretely:

1. Actor A invokes `approval.inbox` with `payload = { "target_surface": "B.tool", "payload": {...} }`. The approval inbox surface is declared `request_response` so A waits for a verdict.
2. Approval node B receives the `deliver` event, decides (LLM, policy, human prompt, rate limit, …).
3. **Approve** — B signs and `POST /v0/invoke`s an envelope `{from: approval, to: B.tool, kind: invocation, payload: <inner>, wrapped: <original_envelope>}`. When the response comes back to B, B turns around and `POST /v0/respond`s with `{from: approval, correlation_id: <A's invocation id>, kind: response, payload: <inner_response_payload>}`. A's `POST /v0/invoke` now returns 200 with the forwarded result.
4. **Deny** — B `POST /v0/respond`s with `{from: approval, correlation_id: <A's invocation id>, kind: error, payload: {"reason": "denied_by_human", ...}}`. A's `POST /v0/invoke` returns 200 with that error envelope.

Approval node's own decision logs are local to that node — they are NOT part of Core's audit.

## 5. Audit log

Core writes one JSON object per line to `audit.log`. Decision codes:

```
routed
denied_no_relationship
denied_signature_invalid
denied_schema_invalid
denied_unknown_node
denied_unknown_surface
denied_node_unreachable
timeout
```

Each entry carries `id`, `timestamp`, `type` (`invocation` or `response`), `from_node`, `to_surface`, `decision`, `correlation_id`, and a free-form `details` map.

## 6. Manifest format

```yaml
nodes:
  - id: <unique node id>
    kind: actor | capability | approval | hybrid
    runtime: <opaque descriptor — local-process, docker:img, external-http, human, ...>
    identity_secret: env:<ENV_VAR>          # or a literal string
    metadata: { ... free-form ... }          # optional. Includes location_hint here, not at top level.
    surfaces:
      - { name: <s>, type: tool|inbox, invocation_mode: request_response|fire_and_forget, schema: <relative path> }

relationships:
  - { from: <node_id>, to: <node_id.surface_name> }
```

Notes:
- `metadata.location_hint` is the right place for free-form host tags (`mac-mini`, `macbook`, `pi-garage`). Core ignores it; it's for humans.
- Schema paths are resolved relative to the manifest file.

## 7. Conformance — what "v0 compatible" means

A node implementation is v0-compatible if it can:

1. Register with the canonical signing rule.
2. Open and parse a `text/event-stream`, dispatching `deliver` events to handlers and ignoring SSE comments / `hello` / heartbeats.
3. Sign every outbound envelope with HMAC-SHA256 over canonical JSON.
4. Receive an invocation, produce a response envelope with `correlation_id = invocation.id`, and `POST /v0/respond`.
5. Optionally, send invocations and handle the synchronous 200/202 / 4xx / 504 responses.

If those work, the ten flows in PRD §7 are reachable. The Python reference implementation in this repo is the conformance test.

## 8. Versioning

The path prefix is `/v0/`. Breaking changes bump it. Additive changes (new optional envelope fields, new audit decision codes) keep `/v0/` and document the addition here.
