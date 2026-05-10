# RAVEN Mesh Wire Protocol — v0

This is the language-agnostic specification of the **RAVEN Mesh
protocol**. It deliberately makes no reference to any specific node, any
specific operator UI, or any specific deployment topology. A reader of
this document should be able to implement either side of the wire — a
Core, or a node — in any language, without consulting any source file
in this repository.

The protocol is the **moat**. Anything in the rest of the repository that
contradicts this document is wrong; this document wins.

> **Substitution test.** If you forked this protocol and built a totally
> different product on it (no kanban, no voice, no dashboard), this
> document should still feel right. If a sentence below only makes sense
> because of one particular tenant, it is a documentation bug — flag it.

## 1. Vocabulary

- **Node** — any participant in the mesh. Has a stable id and an HMAC
  shared secret. A node has no implicit address; addresses are an
  implementation concern of the broker.
- **Surface** — a typed, named entry point on a node.
  Surface id is `{node_id}.{surface_name}`.
  - `type`: `tool` (callable in either request/response or
    fire-and-forget mode) or `inbox` (typically fire-and-forget; may be
    declared request/response when the receiver is a forwarder, e.g. an
    approval-shaped node).
  - `invocation_mode`: `request_response` or `fire_and_forget`.
  - `schema`: a JSON Schema validating the surface's input payload.
- **Relationship** — a directed edge `(from_node, to_surface)`. Edge
  exists ⇒ allowed; no edge ⇒ denied. There is no policy field on the
  edge: presence is the entire ACL. Richer policy belongs in a node.
- **Core** — the broker. Single source of truth for identity, registry,
  routing, schema validation, and audit. Core has no opinions about
  what nodes do; only that they speak the wire.
- **Manifest** — a YAML document declaring nodes, surfaces, and
  relationships. Loaded by Core at startup and on `POST /v0/admin/reload`.

**Envelope kind vs. node kind.** The word "kind" appears in two
distinct senses in the protocol. The **envelope's** `kind` is one of
`invocation`, `response`, `error`. The **node's** `kind` (declared in
the manifest, returned by `/v0/register`) is one of `actor`,
`capability`, `approval`, `hybrid` and is purely descriptive metadata —
Core treats all node kinds identically for routing.

## 2. Envelope

Every message Core handles is a JSON envelope:

```json
{
  "id":              "msg-uuid",
  "correlation_id":  "trace-uuid (= id for the first message in a trace)",
  "from":            "node_id",
  "to":              "node_id.surface_name",
  "kind":            "invocation" | "response" | "error",
  "payload":         { ... schema-validated ... },
  "wrapped":         { ...optional, an inner envelope when this envelope
                       is a forwarding of an earlier one... },
  "timestamp":       "ISO-8601",
  "signature":       "<hex hmac-sha256>"
}
```

`kind` is required. `from`, `to`, `id`, `correlation_id`, `timestamp`,
and `signature` are required for envelopes traveling on `/v0/invoke`
and `/v0/respond`. `payload` is required when the destination surface
declares a non-trivial schema; an empty payload is `{}`.

**The envelope never contains a host, port, URL, or any transport
address.** Routing is by surface id; where a node lives is the broker's
problem. This is what allows nodes to relocate without protocol changes.

The optional `wrapped` field carries an inner envelope when this envelope
is a forwarding of an earlier one. Its primary use today is approval
flows (a forwarder receives an invocation, decides, then forwards an
inner envelope onwards). The field is generic — any forwarder shape can
use it (audit taps, rate limiters, fan-out routers, future patterns we
haven't enumerated). The protocol does not interpret `wrapped`; only the
forwarder and its consumer do.

### 2.1 Signing

The signature covers a canonical JSON serialization of every envelope
field **except `signature`**:

```
canonical(env)  = JSON of (env without "signature"),
                  with sorted keys, no extra whitespace,
                  separators = (",", ":")
signature       = hex( HMAC-SHA256( secret, canonical(env) ) )
```

Concrete reference (Python; trivially portable):

```python
import hmac, hashlib, json
def canonical(env: dict) -> str:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))
def sign(env, secret):
    return hmac.new(secret.encode(), canonical(env).encode(),
                    hashlib.sha256).hexdigest()
```

Verification computes the expected signature with the sender's known
secret and compares using constant-time comparison
(`hmac.compare_digest` in Python).

### 2.2 Registration body

The registration POST is **not** an envelope, but uses the same signing
rule (whole body excluding `signature`):

```json
{
  "node_id":   "tasks",
  "timestamp": "2026-05-09T18:00:00Z",
  "signature": "<hex hmac-sha256>"
}
```

Like envelopes, the registration body never names a host, port, or
URL — the act of `POST`ing it identifies the connection.

## 3. Channels

Two channels per node, both terminated at Core. The transport is HTTP
over TCP. Production deployments are expected to use HTTPS; the protocol
does not mandate TLS.

### 3.1 Outbound — Node → Core

| Endpoint            | Body                                | Purpose |
| ------------------- | ----------------------------------- | ------- |
| `POST /v0/register` | registration body (§2.2)            | declare presence; obtain a session id |
| `POST /v0/invoke`   | full envelope, `kind=invocation`    | route to a target surface |
| `POST /v0/respond`  | full envelope, `kind=response\|error` | resolve a previously-received invocation |

`POST /v0/register` returns:

```json
{
  "session_id":   "<uuid>",
  "node_id":      "tasks",
  "kind":         "capability",
  "surfaces":     [{ "name": "create", "type": "tool",
                     "invocation_mode": "request_response" }, ...],
  "relationships":[{ "from": "tasks", "to": "..." },
                   { "from": "...",   "to": "tasks.create" }, ...]
}
```

A re-registration (same `node_id`) supersedes the prior session: Core
emits a close sentinel into the previous SSE stream and the prior
session id becomes invalid.

`POST /v0/invoke` returns one of:

| HTTP | Body | Meaning |
| --- | --- | --- |
| 200 | response envelope | request_response surface delivered, the target responded in time. |
| 202 | `{"id": "<msg-id>", "status": "accepted"}` | fire_and_forget surface accepted. |
| 400 | `{"error": "denied_schema_invalid", ...}` | payload failed JSON Schema validation. |
| 400 | `{"error": "bad_kind", "expected": "invocation"}` | sent a non-invocation envelope. |
| 400 | `{"error": "bad_surface_id"}` | `to` is missing the `node.surface` shape. |
| 401 | `{"error": "bad_signature"}` | HMAC did not verify. |
| 403 | `{"error": "denied_no_relationship", ...}` | no `(from, to)` edge in the manifest. |
| 404 | `{"error": "unknown_node"}` / `{"error": "unknown_surface"}` | target does not exist in the manifest. |
| 503 | `{"error": "denied_node_unreachable", ...}` | target node is declared but not currently connected. |
| 503 | `{"error": "denied_queue_full", ...}` | target is connected but its delivery queue is full. The queue size is implementation-defined; Cores must surface this discretely. |
| 504 | `{"error": "timeout", "id": "<msg-id>"}` | target did not respond within the invocation timeout. |

`POST /v0/respond` returns 200 `{"status": "accepted"}` or:

| HTTP | Error | Meaning |
| --- | --- | --- |
| 400 | `bad_kind` | response envelope has the wrong `kind`. |
| 400 | `missing_correlation_id` | response did not name an invocation. |
| 401 | `bad_signature` | HMAC did not verify. |
| 403 | `responder_not_target` | the responder is not the node that originally received the invocation. |
| 404 | `no_pending_request` | nothing is waiting for this correlation id. |
| 404 | `unknown_node` | from-node not in manifest. |

A response is accepted only from the node that originally received the
matching invocation. This is what makes forwarding (§4.2) safe: the
forwarder's response resolves the original call only because the
forwarder was the target of the original invocation, not the inner
target.

### 3.2 Inbound — Core → Node

`GET /v0/stream?session=<session_id>` — a long-lived `text/event-stream`
connection. Core pushes events:

```
event: hello
data: {"node_id": "tasks", "session_id": "..."}

event: deliver
data: {"id":"...", "from":"...", "to":"...", "kind":"invocation", ...}

: heartbeat
```

Lines beginning with `:` are SSE comments (heartbeats). Implementations
must tolerate them.

When a node re-registers, Core terminates any prior SSE stream for the
same node id by emitting an internal close sentinel; clients should
expect their stream to end if they `POST /v0/register` again.
`Last-Event-ID` resume is reserved for `v0.x`; in v0 a node simply
re-registers on reconnect.

### 3.3 Read-only

| Endpoint              | Returns |
| --------------------- | ------- |
| `GET /v0/healthz`     | `{"ok": true, "nodes_declared": N, "nodes_connected": M, "edges": E, "pending": P}` |
| `GET /v0/introspect`  | full manifest snapshot — declared nodes, edges, connection state. |

## 4. The two flows you must implement to pass conformance

### 4.1 Tool call

1. Node A registers via `POST /v0/register`, opens its SSE stream.
2. Node A signs and `POST /v0/invoke`s an envelope
   `{from: A, to: B.toolX, kind: invocation, payload: {...}}`.
3. Core verifies signature, checks edge `(A, B.toolX)`, validates
   payload against `B.toolX`'s schema, then **pushes** a `deliver` event
   into B's SSE stream containing the same envelope.
4. Node B reads the deliver event, runs its handler, builds a response
   envelope `{from: B, to: A, correlation_id: <A's invocation id>,
   kind: response, payload: {...}}`, signs it, and `POST /v0/respond`s.
5. Core matches `correlation_id` to A's pending invocation and returns
   the response body to A's open `/v0/invoke` HTTP call.

### 4.2 Forwarding flow

A forwarding node F is just a node. Its role in the protocol:

1. Some actor A invokes `F.surface` — typically declared
   `request_response` so A waits for a verdict. Payload typically
   includes a `target_surface` and a payload to forward (the schema is
   the forwarder's, not the protocol's).
2. F receives the `deliver` event and decides — by policy, by human
   prompt, by rate-limit, by LLM, by anything. The protocol does not
   constrain or witness F's decision.
3. **Approve.** F signs and `POST /v0/invoke`s a new envelope
   `{from: F, to: <inner_target>, kind: invocation,
     payload: <inner_payload>, wrapped: <original_envelope>}`.
   When the inner response arrives, F `POST /v0/respond`s with
   `{from: F, correlation_id: <A's id>, kind: response,
     payload: <inner_response_payload>}`. A's `POST /v0/invoke` now
   returns 200 with the forwarded result.
4. **Deny.** F `POST /v0/respond`s with `{from: F,
     correlation_id: <A's id>, kind: error, payload: {"reason": ...}}`.

A forwarder's **internal** decisions are local to that node — they are
not part of Core's audit log. Core audits only the routing decisions on
the edges A→F and F→inner.

Approval gates are the canonical example of a forwarder. Audit taps,
fan-out routers, and rate-limiters are other forwarder shapes the
protocol natively supports without modification.

## 5. Audit log

Core writes one JSON object per line to its audit log. Decision codes:

```
routed
denied_no_relationship
denied_signature_invalid
denied_schema_invalid
denied_unknown_node
denied_unknown_surface
denied_node_unreachable
denied_queue_full
timeout
```

Each entry carries `id`, `timestamp`, `type` (`invocation` or
`response`), `from_node`, `to_surface`, `decision`, `correlation_id`,
and a free-form `details` map.

The audit log records routing decisions only; it is not a transport log
and does not store payloads in full. Implementations are free to
configure where the log lands and how it is rotated.

## 6. Manifest format

```yaml
nodes:
  - id: <unique node id>
    kind: actor | capability | approval | hybrid
    runtime: <opaque descriptor — any string a deployment finds useful>
    identity_secret: env:<ENV_VAR>          # or a literal string
    metadata: { ... free-form ... }          # optional. Free-form keys.
    surfaces:
      - { name: <s>, type: tool|inbox,
          invocation_mode: request_response|fire_and_forget,
          schema: <relative path to JSON Schema> }

relationships:
  - { from: <node_id>, to: <node_id.surface_name> }
```

### 6.1 Validation

A conforming Core MUST reject (refuse to load) a manifest that:

- Is not a top-level YAML mapping.
- Declares duplicate node ids.
- Declares a node id of `core` (reserved for the broker's own future
  surfaces).
- Declares two surfaces with the same name on a single node.
- References a surface schema file that does not exist or does not
  parse as JSON.
- Has a relationship whose `from` references an undeclared node.
- Has a relationship whose `to` is not of the form `node.surface`, or
  whose target node or target surface is undeclared.

A conforming Core SHOULD warn (but may proceed) when:

- An `identity_secret: env:VAR` references an environment variable that
  is unset. The Core's behavior in this case (reject vs. autogenerate)
  is implementation-defined; both are conformant.

The canonical machine-readable manifest schema is in
`schemas/manifest.json`.

### 6.2 Notes

- `runtime` is opaque. The protocol does not interpret it. Common
  values include `local-process`, `external-http`, `human`, `docker:img`,
  and so on. Cores MAY use this field to drive process supervision; the
  protocol does not require them to.
- `metadata` is free-form and never enforced by the protocol. Operator
  conventions like `metadata.location_hint` (host tags) live here.
- Schema paths are resolved relative to the manifest file.

## 7. Conformance — what "v0 compatible" means

A node implementation is v0-compatible if it can:

1. Register with the canonical signing rule (§2.1, §2.2).
2. Open and parse a `text/event-stream`, dispatching `deliver` events
   to handlers and ignoring SSE comments and `hello` frames.
3. Sign every outbound envelope with HMAC-SHA256 over the canonical
   JSON form.
4. Receive an invocation, produce a response envelope with
   `correlation_id = invocation.id`, and `POST /v0/respond`.
5. Surface 4xx, 503 (including `denied_queue_full`), 504, and 429 (if
   admin-namespace; see §9) results to its caller without crashing.
6. Optionally — required only for actor-shaped nodes — send invocations
   and handle the synchronous 200/202/4xx/5xx responses.

A Core implementation is v0-compatible if it:

- Exposes the endpoints in §3 with the documented bodies and statuses.
- Validates manifests per §6.1.
- Writes the audit log per §5.
- Rejects unsigned, malformed, or unauthorized envelopes.
- Optionally — required only if it claims to host the admin namespace —
  exposes §9 and rate-limits it per §9.4.

The reference Python implementation in this repository's `core/`
directory passes a battery of tests in `tests/test_protocol.py`. A
fresh Core in another language is conformant when those tests pass
against it (port-pointed at the new Core).

## 8. Versioning

The path prefix is `/v0/`. Breaking changes — changes that would cause
a previously-conforming v0 client to fail — bump the prefix.

Additive changes keep `/v0/` and are documented here. The following
recent additions are additive: queue-full and rate-limit error codes
(existing well-behaved callers do not encounter them under normal
operation), the optional admin namespace (§9) which a Core may choose
not to expose, and supervisor-related admin endpoints which are
optional even within the admin namespace.

## 9. Admin namespace (optional)

A v0-conforming Core MAY expose an admin namespace at `/v0/admin/*` for
operators. The admin namespace is **not required for protocol
conformance**: a Core that exposes only `/v0/register`, `/v0/invoke`,
`/v0/respond`, `/v0/stream`, `/v0/healthz`, and `/v0/introspect` is
conformant.

The endpoints below describe what a Core MUST do *if* it exposes them.
Operator UIs that target the admin namespace must be prepared for it
to be absent.

### 9.1 Authentication

Every admin endpoint requires an `X-Admin-Token` header equal to a
secret configured at Core start. Tokens MUST NOT be accepted in the
query string or in cookies (those leak through shell history, browser
history, server logs, and Referer headers). A Core SHOULD refuse to
start with an unset or known-default token.

### 9.2 Endpoints

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/v0/admin/state` | GET | Full snapshot: declared nodes, edges, connection state, the most recent envelopes, voluntarily-reported UI state. |
| `/v0/admin/stream` | GET | Live SSE tap of every envelope routed by Core, plus a replay of the most recent N envelopes on connect. |
| `/v0/admin/manifest` | POST | Replace the manifest YAML on disk. Core MUST validate the new manifest before accepting it; on failure, restore the prior manifest. |
| `/v0/admin/reload` | POST | Re-read the manifest currently on disk. |
| `/v0/admin/invoke` | POST | Synthesize a signed envelope from a chosen registered node id and route it. Equivalent to that node having sent the invocation itself. |
| `/v0/admin/node_status` | POST | Voluntary UI-visibility report from a node (e.g. its operator-facing window is shown/hidden). |
| `/v0/admin/ui_state` | GET | Read all reported UI states. |

Cores MAY expose additional endpoints under `/v0/admin/` for
implementation-specific concerns (process supervision, metrics,
diagnostics). Such endpoints are not part of this specification.

### 9.3 Audit and provenance

`POST /v0/admin/invoke` synthesizes an envelope on behalf of a real
node. The audit log entry MUST be indistinguishable from one produced
by that node directly. Operators relying on audit-log forensics must
treat the admin token as a privilege equivalent to every node's
HMAC secret combined.

### 9.4 Rate limiting

Cores SHOULD apply a token-bucket rate limit scoped to the entire
`/v0/admin/*` namespace. When a request is rejected for rate, Core
responds 429 with `{"error": "rate_limited", "scope": "admin"}` and a
`Retry-After` header.

Rate limiting on the non-admin endpoints (`/v0/invoke`, `/v0/respond`,
etc.) is not part of this specification. Per-edge rate limits, when
needed, belong in a forwarder node — not in Core.

---

The protocol stops here. Anything not described above — process
supervision, dashboards, presentation, scheduling, memory, voice,
agents, kanban — belongs in nodes built on top of this protocol, not
in the protocol itself.
