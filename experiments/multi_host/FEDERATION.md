# RAVEN Mesh — Multi-Host Federation Prototype

This document specifies a peer-link protocol that federates multiple RAVEN
Mesh Core processes so a node hosted on `Core A` can invoke a surface on a
node hosted on `Core B`. It is built as a shim (`peer_link.py`) over the
unmodified production `core/` package, with a working two-Core demo at
`experiments/multi_host/`.

The shim is roughly 500 lines plus a 250-line end-to-end test harness. It
runs on stock Python 3.11+ with `aiohttp`, `pyyaml`, and `jsonschema` (all
already direct deps of the existing Core).

---

## 1. Goals and non-goals

**Goals**

* `alpha @ Core A` invokes `beta.ping @ Core B` as if both lived on the same
  Core: same SDK call, same response shape.
* No changes to `core/` source. The shim subclasses behavior by reusing the
  production handlers and replacing `/v0/invoke`.
* Peer authentication is end-to-end: even a malicious *third* peer cannot
  forge envelopes that look like they came from a legitimate peer.
* Cross-host replay, time-skew, and chain-forgery attacks are explicitly
  defeated.

**Non-goals (for v0)**

* Discovery: peers are **statically configured** in the manifest. mDNS or a
  registry can be layered on top later.
* Multi-hop routing: `Core A → Core B → Core C` is out of scope. Each
  remote-node entry binds to exactly one peer.
* Streaming responses across hosts (no SSE-over-peer). The peer-link is
  request/response or fire-and-forget; if a future surface emits server
  push, that pattern needs a separate channel.
* Large-payload optimization: peer envelopes carry inner JSON inline, so
  multi-MB payloads pay double serialization cost.

---

## 2. Manifest extensions

Two new top-level YAML keys are recognized. Existing keys (`nodes`,
`relationships`) are unchanged.

```yaml
local_core_name: A      # logical name for THIS Core. Sent as peer_from.

peer_cores:
  - name: B                          # logical name of the peer Core
    url: http://127.0.0.1:8001
    peer_secret: env:PEER_AB_SECRET  # shared HMAC, this side <-> peer

remote_nodes:
  - id: beta              # node hosted on a peer Core
    peer: B
    kind: capability
    surfaces:
      - name: ping
        type: tool
        invocation_mode: request_response
        schema: ../../schemas/echo.json
```

`remote_nodes` are folded into `state.nodes_decl` as **stub** entries with a
sentinel `secret` (`__remote_no_local_verify__`). This lets the existing
edge-check logic (`(from_node, to)`) work without any Core changes:
relationships referencing remote nodes resolve cleanly because Core only
compares node-ID strings — it never touches a remote node's connection.

The manifest schema already declares `additionalProperties: true` at every
level, so the new keys validate without any schema bump.

Both manifests in a federation pair declare each other's nodes:

* `manifestA.yaml`: alpha is local; beta is a remote_node owned by peer B.
* `manifestB.yaml`: beta is local; alpha is a remote_node owned by peer A.

The relationship `(alpha, beta.ping)` appears in **both** manifests. Edge
authority is local to the surface owner (Core B): if alpha and beta are
unrelated in B's manifest, the call is denied at B regardless of A's view.

---

## 3. Wire protocol

### 3.1 Inner envelope

Unchanged from PRD §5. `node_sdk` builds and signs envelopes the same way
it always has.

```json
{
  "id": "...",
  "correlation_id": "...",
  "from": "alpha",
  "to": "beta.ping",
  "kind": "invocation",
  "payload": { ... },
  "timestamp": "2026-05-10T07:59:58.013931+00:00",
  "signature": "<HMAC-SHA256(canonical(env), alpha_secret)>"
}
```

### 3.2 Peer envelope

A new transport envelope wraps the inner envelope between Cores.

```json
{
  "peer_from": "A",
  "peer_to":   "B",
  "nonce":     "<random uuid hex>",
  "timestamp": "2026-05-10T07:59:58.013931+00:00",
  "inner":     { ...inner envelope, signature included... },
  "signature": "<HMAC-SHA256(canonical(peer_env), peer_AB_secret)>"
}
```

The signature covers the canonical JSON of every other field, including the
inner envelope's full body. Tampering with `inner.payload` or the inner
signature invalidates the outer signature — see test 9 in `run_demo.py`.

Endpoint: `POST /v0/peer/envelope`. The HTTP body is the peer envelope.
The HTTP response body is whatever Core B's `_route_invocation` returned:
either the response envelope from the target node, or an error.

For fire-and-forget surfaces, B returns `202 {"id": "...", "status":
"accepted"}` — exactly the same shape `_route_invocation` already returns
locally — and the originating Core relays it verbatim.

---

## 4. Sequence diagrams

### 4.1 Happy path: alpha @ A invokes beta.ping @ B

```
alpha               Core A                                   Core B               beta
  |                   |                                        |                   |
  |--POST /v0/invoke->|                                        |                   |
  |  E_alpha          |  verify alpha_secret                   |                   |
  |  (HMAC alpha)     |  edge (alpha, beta.ping) declared OK   |                   |
  |                   |  beta in remote_nodes[B]               |                   |
  |                   |  build peer envelope P:                |                   |
  |                   |   { peer_from:A, peer_to:B,            |                   |
  |                   |     nonce, ts, inner:E_alpha }         |                   |
  |                   |  P.signature = HMAC(P, peer_AB)        |                   |
  |                   |                                        |                   |
  |                   |---POST /v0/peer/envelope (P)---------->|                   |
  |                   |                                        | verify peer_AB    |
  |                   |                                        | nonce fresh?      |
  |                   |                                        | ts within ±5min?  |
  |                   |                                        | inner.from=alpha  |
  |                   |                                        |   owned by A?     |
  |                   |                                        | edge declared?    |
  |                   |                                        | schema validate   |
  |                   |                                        | E_alpha           |
  |                   |                                        |--SSE deliver---->|
  |                   |                                        |                   | run handler
  |                   |                                        |<--POST /v0/respond|
  |                   |                                        |  R_beta (HMAC beta)
  |                   |                                        | match pending     |
  |                   |<------HTTP 200 R_beta------------------|                   |
  |<-HTTP 200 R_beta--|                                        |                   |
```

### 4.2 Failed signature chain (forged peer HMAC)

```
attacker            Core B
  |                   |
  |--POST /v0/peer/   |
  |   envelope------->|
  |  P with bogus sig |
  |                   |  HMAC verify fails
  |<--HTTP 401--------|
```

### 4.3 Replay (same nonce twice)

```
attacker            Core B
  |                   |
  |--POST P (nonce=N)|
  |                   |  remember(N) -> fresh, route, return 200
  |<--HTTP 200--------|
  |                   |
  |--POST P again-----| (replay capture)
  |                   |  remember(N) -> already seen
  |<--HTTP 409--------|
```

---

## 5. Trust model and threat analysis

The demo uses per-pair shared HMAC secrets (`peer_secret` per peer entry).
This is the simplest design that scales to a small known federation, and
mirrors the existing per-node `identity_secret` contract. Production
deployments should upgrade to asymmetric keys (§7).

### Attacks mitigated

| # | Attack                                       | Defense                                                              |
|---|----------------------------------------------|----------------------------------------------------------------------|
| 1 | Off-path passive observer reads payloads     | Run the peer link over TLS or Tailscale (transport-level concern)    |
| 2 | Off-path active attacker forges peer envelope| HMAC over canonical JSON; attacker has no `peer_AB_secret`           |
| 3 | Off-path tampers with `inner.payload`        | Outer HMAC binds inner; tamper invalidates signature                 |
| 4 | On-path captures + replays whole envelope    | Per-`(peer_from, nonce)` cache, 10 min TTL                           |
| 5 | On-path captures + replays AFTER nonce GC    | Outer `timestamp` enforced within ±5 min                             |
| 6 | Inner stale: A captured an old alpha env, replays| Inner `timestamp` also checked at ±5 min                         |
| 7 | Peer A claims sender is `beta` (impersonates B's local node) | Inner `from` must be a `remote_node` owned by `peer_from` |
| 8 | Peer A invokes a surface alpha has no relation to | Edge check at A *and* at B; B is authoritative                  |
| 9 | Peer A bad-payload spam against B            | Schema validation at A pre-flight; Core B re-validates               |

### Attacks NOT mitigated

| # | Attack                                       | Why not                                                              |
|---|----------------------------------------------|----------------------------------------------------------------------|
| A | Compromised Core A forges any inner envelope from any of A's nodes | Trust boundary: A *is* its nodes' authority. Inner sig is not verified at B for peer-delivered envelopes. |
| B | Side-channel timing of HMAC compare on B      | Use `hmac.compare_digest` (already does)                            |
| C | Peer A floods B with valid envelopes (DoS)    | Out of scope. Layer rate-limiting at HTTP middleware.               |
| D | Peer A spoofs source IP and reuses peer_AB key after compromise | Add IP allowlist or rotate key. Asymmetric keys (§7) make rotation cleaner. |
| E | Long-lived `peer_secret` rotation             | Manifest reload picks up new secret; coordinated swap needed         |
| F | DNS rebinding on peer URL                     | Validate peer cert (TLS) or pin IP                                   |

The most important boundary is **(A)**: a Core fully vouches for every
envelope it forwards. If Core A is compromised, every node A hosts is
effectively compromised from B's perspective. This is the same trust model
single-host mesh has today, just lifted to the Core-pair level.

---

## 6. Comparison with adjacent technologies

| Tech              | Identity & auth                  | Routing model                    | Where mesh fits                                      |
|-------------------|----------------------------------|----------------------------------|------------------------------------------------------|
| **NATS leaf nodes** | Account JWT, signed by an operator | Subject-based pub/sub fanout   | Mesh is request/response with explicit per-edge ACLs; leaf-node accounts are coarse |
| **libp2p**        | Each peer has a PeerID (Ed25519 pubkey) | DHT-driven mesh routing      | libp2p is a great future replacement for peer transport; mesh's surface contract sits one layer above |
| **Tailscale (as transport)** | WireGuard tunnel, machine identity | Flat L3 network              | Tailscale solves *transport security and reachability*; mesh's peer link runs *over* it. Strong production fit |
| **gRPC mTLS**     | x509 client certs + CA           | RPC method per service           | gRPC is a closer cousin: typed surfaces, request/response, mTLS-bound identity. Mesh trades schema strictness (jsonschema vs protobuf) for SDK-light dynamic surfaces |
| **MQTT bridges**  | Username+password or TLS         | Topic-based fanout               | Topic semantics don't map well to mesh's `node.surface` invocation model |
| **Consul Connect**| Workload identity SPIFFE         | Service mesh sidecar             | Heavy. Mesh's Core *is* the sidecar — collapsed into one process per host |

**Where mesh wins**: the manifest is the source of truth for the entire
mesh shape (nodes, surfaces, edges, schemas). Federation just means
splitting that manifest across hosts while keeping its semantics intact.
You don't need to learn a separate ACL language, a separate IDL, and a
separate routing layer.

**Where mesh loses (today)**: NATS, libp2p, and gRPC have decades of
production hardening. The peer-link in this prototype is ~500 LOC; it's a
demonstration, not a defensible production system.

---

## 7. Migration path: what does single-host mesh need to become federation-ready?

Today (single-host) → federation-ready in roughly five steps:

1. **Manifest schema additions** (zero-cost). Add `local_core_name`,
   `peer_cores`, `remote_nodes` to `schemas/manifest.json`. The validator
   already accepts unknown keys, but explicit support gives operators
   error messages.

2. **Refactor `_route_invocation`** to expose a hook before the
   `state.connections.get(target_node)` lookup. Today the shim works
   around this by replacing the `/v0/invoke` handler entirely; folding
   the remote-target check into Core lets `/v0/admin/invoke` and any
   future invocation entry points share the federation path.

3. **Identity upgrade**: replace shared `peer_secret` with Ed25519
   keypairs. Each Core advertises a `peer_pubkey` in its `peer_cores`
   entries. Sign peer envelopes with `peer_privkey`. This eliminates the
   shared-secret rotation problem and lets a peer's pubkey be served from
   a well-known endpoint (`/v0/peer/info` already exists for this).

4. **Discovery**: optional `peer_discovery: mdns | static | registry` on
   the manifest. mDNS is fine for local-network demos; a small
   registration registry (or DNS SRV records) covers fleet deployments.

5. **Transport hardening**: terminate the peer link inside a Tailscale
   tailnet, or run it behind a TLS reverse proxy with mTLS. The peer-
   envelope HMAC is *application-layer* auth and cannot be relied on for
   confidentiality.

Beyond that, the SDK does not change at all: alpha calls
`node.invoke("beta.ping", payload)` whether beta is local or remote. The
*manifest* is the only thing that distinguishes the two cases. This is the
right separation: the application-layer code never has to think about
topology.

A noteworthy consequence: because the SDK is unchanged, every existing
node implementation (voice_actor, kanban_node, nexus_agent…) becomes
federation-ready the moment its Core's manifest gains `peer_cores` /
`remote_nodes` entries. No code rewrite needed.

---

## 8. Operational considerations

* **Audit log**: each Core continues to write its own `audit.log`. Peer
  forwards add `type: peer_forward` events at A and `type: peer_inbound`
  events at B; correlation IDs let an operator stitch a cross-host trace
  back together by joining on `correlation_id`.

* **Dashboard**: the existing `/v0/admin/state` and `/v0/admin/stream`
  endpoints continue to work per-Core. A federated dashboard would
  multiplex across Cores by polling each `/v0/admin/state` and merging on
  shared node IDs (with the peer-name as a discriminator for collisions).

* **Health & failover**: if peer B is unreachable, A returns `502
  peer_unreachable` to the calling node within `MESH_PEER_FORWARD_TIMEOUT`.
  Active health-checks (`GET /v0/healthz`) on each peer would let A
  short-circuit forwards to a known-down peer.

* **Capacity**: nonce cache is in-memory. Bound is `peer_count * ttl *
  rate`. At 10 peers × 600s × 100 req/s that's 600k entries — fine. For
  larger fleets, swap in Redis or LRU.

* **Backpressure**: the peer link is HTTP request/response, so peer B's
  awaiting future has whatever timeout `_route_invocation` enforces
  (`MESH_INVOKE_TIMEOUT`, default 30s). The originating Core's HTTP client
  uses `MESH_PEER_FORWARD_TIMEOUT` (default 35s) — slightly longer so it
  always sees the inner timeout error rather than its own.

---

## 9. Running the demo

```bash
# From RAVEN_MESH repo root.
python -m experiments.multi_host.run_demo
```

This boots both Cores on `127.0.0.1:8000` and `127.0.0.1:8001`, starts
alpha and beta, and runs ten assertions covering the happy path, slow
surface, bad inner signature, missing edge, replay, time-skew, forged
peer HMAC, peer impersonation, payload tampering, and the diagnostic
`/v0/peer/info` endpoint.

To run the Cores manually for ad-hoc poking:

```bash
# terminal 1
ALPHA_SECRET=alpha-secret PEER_AB_SECRET=peer-ab \
    python -m experiments.multi_host.peer_core \
    --manifest experiments/multi_host/manifestA.yaml --port 8000

# terminal 2
BETA_SECRET=beta-secret PEER_AB_SECRET=peer-ab \
    python -m experiments.multi_host.peer_core \
    --manifest experiments/multi_host/manifestB.yaml --port 8001

# terminal 3
ALPHA_SECRET=alpha-secret \
    python -m experiments.multi_host.nodes.alpha --core-url http://127.0.0.1:8000

# terminal 4
BETA_SECRET=beta-secret \
    python -m experiments.multi_host.nodes.beta --core-url http://127.0.0.1:8001

# terminal 5 — invoke beta.ping from alpha via Core A's admin synthesizer.
# Note: admin/invoke goes through _route_invocation directly, not the
# federated /v0/invoke wrapper. Use /v0/invoke with a node-signed envelope
# (see run_demo._signed_invoke) for a true federated request.
```

---

## 10. File map

| File                                          | Role                                                     | LOC   |
|-----------------------------------------------|----------------------------------------------------------|-------|
| `peer_link.py`                                | Federation shim: state, handlers, peer envelope helpers  | ~470  |
| `peer_core.py`                                | CLI entry point. Same args as `core.core` + manifest     | ~60   |
| `manifestA.yaml` / `manifestB.yaml`           | Two-Core demo topology                                   | ~50×2 |
| `nodes/alpha.py`                              | alpha actor (idle, demo driver invokes through it)       | ~45   |
| `nodes/beta.py`                               | beta capability with `ping` and `slow` surfaces          | ~70   |
| `run_demo.py`                                 | End-to-end orchestrator + ten failure-mode assertions    | ~280  |
| `FEDERATION.md`                               | This document                                            | ~     |

Total Python ~1000 LOC. No production-tree changes.
