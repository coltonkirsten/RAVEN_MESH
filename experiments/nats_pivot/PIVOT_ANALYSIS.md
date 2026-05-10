# NATS pivot: should RAVEN_MESH be built on nats.io?

**TL;DR.** A working NATS-backed equivalent of `core/core.py` + `node_sdk` is
~44% the line count and ~25% lower at p95 invoke latency on loopback. NATS
covers transport, auth, ACL, fan-out, durable replay, and reconnect — all
features the mesh hand-rolls. **But** the mesh's value is not the transport;
it is the **manifest as the typed source of truth** (schemas, edges, surface
kinds, invocation modes, supervisor lifecycle). NATS gives you the wires; it
doesn't give you typed surfaces, declarative schema validation, or a single
artifact you can read to understand "who can call whom for what." The right
move is **hybrid**: keep the manifest and the SDK ergonomics, swap the
in-process broker for NATS *under the SDK* when (and only when) we need to
peer-federate across hosts. This document walks the prototype, the
side-by-side, the perf numbers, and the cost/benefit.

---

## 1. Mapping table — every mesh primitive in NATS

| Mesh primitive (current `main`) | NATS construct | Where it lives now |
| --- | --- | --- |
| Envelope routing (`{from,to,kind,payload,...}` over `POST /v0/invoke`) | NATS subject `mesh.<from>.<to_node>.<surface>` | `nats_core.invoke_subject` / `nats_node_sdk.invoke` |
| Correlation IDs (`id`, `correlation_id`) | NATS reply-inbox (`_INBOX.<token>`) auto-allocated by `nc.request()` | `nats_node_sdk.invoke` (the `await self.nc.request(...)`) |
| HMAC signing (`sign(canonical(env), secret)`) | NATS user/password auth (production: NKey signed CONNECT) | `nats_core.derive_password`; users block in `nats.conf` |
| `identity_secret: env:FOO` | NATS user credential, derived deterministically from manifest | `nats_core.derive_password` |
| Allow-edges (`relationships: [{from, to: node.surface}]`) | NATS user **publish-permission allow-list** | `nats_core.compile_nats_config` (`pubs = [...]`) |
| Surface ownership (a node owns `node.surface`) | NATS user **subscribe-permission allow-list** with `mesh.*.<self>.<surface>` | `nats_core.compile_nats_config` (`subs = [...]`) |
| Fire-and-forget surfaces | `nc.publish(subj, body)` (no reply expected) | `nats_node_sdk.invoke(wait=False)` |
| Request/response surfaces | `nc.request(subj, body, timeout=…)` | `nats_node_sdk.invoke(wait=True)` |
| `/v0/register` handshake → `session_id` | Implicit: connecting authenticates the user; subscriptions become the registration | (gone) |
| `/v0/stream` SSE delivery to nodes | NATS subscriptions (push consumers) | `nats_node_sdk.start` (`self.nc.subscribe(...)`) |
| `/v0/respond` (responder publishes back) | `nc.publish(msg.reply, body)` | `nats_node_sdk._make_dispatch` |
| Audit log (JSON-per-line on disk) | JetStream stream `MESH_AUDIT` over `audit.>` + a tail subscriber | `nats_core.setup_audit_stream`, `tail_audit` |
| Envelope tail (admin SSE tap) | A `nc.subscribe("audit.>")` from anywhere (the dashboard *is* the tap) | `nats_core.tail_audit` is one consumer; any number more can attach |
| Hot manifest reload (`POST /v0/admin/manifest`) | Regenerate `nats.conf`, send `SIGHUP` to `nats-server` (ACL changes apply live) | sketched, not in this prototype |
| Supervisor reconcile (Core spawns/stops node processes) | **Not** an NATS concern — orthogonal | unchanged; would still live in mesh |
| JSON Schema validation per surface | **Not** an NATS concern — must live in node SDK | `nats_node_sdk._make_dispatch` (calls `jsonschema_validate` before handler) |
| Manifest-as-source-of-truth | **Not** an NATS concern — must live above NATS | `nats_core.compile_nats_config` reads the manifest |

**Translation rules in one sentence each.**
- An *invocation* is a publish to `mesh.<self>.<peer>.<surface>` with a reply
  inbox; the broker rejects the publish unless the manifest gave that exact
  edge.
- A *surface* is a subscription to `mesh.*.<self>.<surface>`; the broker
  rejects the subscribe unless the node owns that surface.
- A *response* is a publish back to the request's reply inbox; permissions
  on `_INBOX.>` cover this for free.
- The *audit log* is a JetStream stream over `audit.>`; every node SDK
  republishes a normalised audit copy (`audit.<self>.<from>.<surface>.<decision>`)
  after it handles a message.

---

## 2. The working prototype

Everything is at `experiments/nats_pivot/`:

```
nats_pivot/
├── manifest.yaml          # 3 nodes: echo, kanban, dashboard
├── schemas/
│   ├── echo.json
│   ├── kanban_create.json
│   └── kanban_list.json
├── nats_core.py           # broker config compiler + audit subscriber (296 LOC)
├── nats_node_sdk.py       # NatsNode (215 LOC)
├── nodes/
│   ├── echo_node.py       # 27 LOC — capability with one surface
│   ├── kanban_node.py     # 43 LOC — capability with two surfaces, in-mem state
│   └── dashboard_node.py  # 44 LOC — pure caller (does the demo invocations)
├── run_demo.py            # spawns nats-server + 2 nodes + dashboard once (68 LOC)
├── bench.py               # HTTP vs NATS p50/p95 (196 LOC)
├── bench_manifest.yaml    # mirror manifest used by the existing HTTP core
└── run_logs/
    ├── nats.conf          # auto-generated from the manifest
    ├── audit.jsonl        # audit subjects after run
    ├── jetstream/         # MESH_AUDIT JetStream store
    ├── nats-server.log
    └── bench.json
```

`run_demo.py` end-to-end output (verbatim):

```
[run_demo] broker up (port 4233); audit -> .../run_logs/audit.jsonl
[run_demo] running dashboard ...
[dashboard] up
[dashboard] echo.ping        -> {payload: {echo: 'hello mesh', responder: 'echo'}}
[dashboard] kanban.create    -> {payload: {card: {id: c20fda12, title: 'first card', ...}, count: 1}}
[dashboard] kanban.create    -> {payload: {card: {id: 4c3fe4ad, title: 'second card', ...}, count: 2}}
[dashboard] kanban.list      -> {payload: {cards: [...], count: 2}}
[dashboard] kanban.create(bad) -> {kind: error, payload: {reason: denied_schema_invalid, ...}}
[dashboard] echo.unknown_surface -> timeout invoking echo.unknown_surface (denied by broker ACL)
```

Five things to notice:

1. **Routing works without a broker process owning routing.** `nats-server`
   is a generic broker; the `mesh.<from>.<to_node>.<surface>` shape just
   *happens* to be unambiguous, so each subscription gets exactly the messages
   it should.
2. **ACL is enforced at the wire layer.** The `dashboard` user has no edge to
   `echo.unknown_surface`, so the publish is rejected with a `Publish
   Violation` in the server log; the client times out. No mesh code ran for
   that path. With the current mesh, the equivalent denial happens *inside*
   `core/_route_invocation` after the envelope is parsed.
3. **Schema validation happened on the responder.** Because there is no
   in-band broker that holds the schemas, the SDK on the receiving side must
   validate the payload before dispatch. That's a real loss of a property the
   mesh has today: schema rejections are not "free" — every node has to use
   the SDK or re-implement validation.
4. **Audit is durable replay, not a flat file.** The `MESH_AUDIT` JetStream
   stream survives restarts; any new consumer can replay the conversation
   from `seq=1`. The current `audit.log` cannot replay — it can only be
   `tail -f`'d.
5. **No `/v0/register` handshake.** Connecting to NATS *is* the registration.
   The mesh's session table goes away.

### Generated NATS config (excerpt)

```hocon
authorization {
  users: [
    { user: "echo", password: "...",
      permissions: { publish: { allow: ["_INBOX.>", "audit.echo.>"] },
                     subscribe: { allow: ["mesh.*.echo.ping", "_INBOX.>"] } } }
    { user: "kanban", password: "...",
      permissions: { publish: { allow: ["_INBOX.>", "audit.kanban.>"] },
                     subscribe: { allow: ["mesh.*.kanban.create",
                                          "mesh.*.kanban.list",
                                          "_INBOX.>"] } } }
    { user: "dashboard", password: "...",
      permissions: { publish: { allow: ["mesh.dashboard.echo.ping",
                                        "mesh.dashboard.kanban.create",
                                        "mesh.dashboard.kanban.list",
                                        "_INBOX.>",
                                        "audit.dashboard.>"] },
                     subscribe: { allow: ["_INBOX.>"] } } }
    { user: "audit", password: "...",
      permissions: { subscribe: { allow: ["audit.>", "$JS.>", "_INBOX.>"] },
                     publish: { allow: ["$JS.>", "_INBOX.>"] } } }
  ]
}
```

The whole file is ~30 lines and is **derived mechanically from
`manifest.yaml`** by `nats_core.compile_nats_config`. That is the single
biggest "for free" win: the manifest stays the source of truth and the
broker's enforcement is *the literal manifest*, not a parallel rule
engine inside `core/_route_invocation`.

---

## 3. What did NATS give us for free, what did we lose

### Free (worth it)

| Capability | NATS gives | Mesh today does |
| --- | --- | --- |
| **Reconnect / heartbeat** | nats-py + nats-server handle reconnect, server-list failover, heartbeats | Hand-rolled SSE keep-alive, `_close` event, no client-side reconnect |
| **Fan-out / queue groups** | `subscribe(subj, queue="g")` distributes load across N replicas | Not supported — one connection per node-id |
| **Durable replay** | JetStream stream + durable consumer — replay from any seq, redeliver on no-ack | `audit.log` is append-only text; no replay |
| **Ack / redelivery semantics** | JS `ack`, `nak`, `term` with backoff | Not modelled |
| **Multi-host transport** | NATS clusters / leaf nodes / gateways | Mesh has no peer story today; you'd build one |
| **Wire-level ACL** | publish/subscribe permissions, evaluated at the broker for every message | Mesh checks `(from, to) in edges` after parsing — same outcome, more code |
| **Operator tooling** | `nats` CLI, `nats-top`, JS metrics, NATS Surveyor | Hand-built admin endpoints + dashboard |
| **Battle-tested** | nats-server is in production at scale | Mesh is a v0 |

### Lost (real costs)

| What we lose | Why it matters | Mitigation |
| --- | --- | --- |
| **Custom envelope shape** | Today every routed message has `{id, correlation_id, from, to, kind, payload, signature, timestamp, wrapped}`. NATS just routes opaque bytes; the envelope becomes app-level convention. | Keep the envelope shape inside `nats_node_sdk` — the prototype already does. But cross-language nodes must replicate it. |
| **Centralised schema validation** | Today, an envelope that fails JSON Schema is rejected by Core *before* it reaches the responder. Audit logs the denial uniformly. | Validation moves to the responder SDK. Cross-language nodes must validate themselves or accept that bad payloads reach handler code. |
| **Broker as the single audit witness** | One file, every event, every denial reason in one shape. | JS audit stream gets the events but the *denials at the broker layer* (ACL violations) only show up in nats-server logs, not in the audit stream. Recovering full coverage means scraping nats-server logs or using `$SYS` events. |
| **`signature_valid` as a first-class field** | The mesh emits per-envelope signature_valid in admin tap and audit. With NATS auth, the question doesn't exist — if you connected, you're the user. | Probably fine; arguably more honest. |
| **Operator complexity** | One Python process vs. a Python process + a Go binary + a JetStream filestore + a regenerated config. | Real cost. The brew install is one command, but ops on prod is now "two daemons, one of them with persistent state on disk." |
| **Wrapped envelopes** | The mesh SDK supports `wrapped=original_env` for "I'm forwarding this on behalf of someone else." | Still works (it's just a field in the envelope), but loses any broker-side validation of the wrapping. |
| **Hot reload semantics** | `POST /v0/admin/manifest` swaps edges instantly with backup-on-failure. | NATS reload is `SIGHUP`; should work but the prototype doesn't wire it up. ACL changes are live; existing connections keep their old permissions until the server re-evaluates (verified behaviour, not in this prototype). |
| **Schema-typed introspection** | `GET /v0/admin/state` returns nodes + surfaces + JSON schemas + edges. | The manifest is still the source; you just can't ask the *broker* what surfaces exist — you ask the manifest file. |

### Lines of code

| File | LOC | Role |
| --- | --- | --- |
| `core/core.py` (mesh) | **875** | broker: register/invoke/respond/stream/admin/CORS/sup wiring |
| `node_sdk/__init__.py` (mesh) | **289** | client SDK |
| **mesh broker + SDK** | **1164** | |
| `nats_core.py` (this experiment) | **296** | manifest -> nats.conf compile, server spawn, audit subscriber |
| `nats_node_sdk.py` (this experiment) | **215** | NATS-backed client SDK with schema validation |
| **NATS broker + SDK** | **511** | ~44% of mesh |

Caveats: `core/core.py` includes admin endpoints (manifest hot-reload,
state snapshot, SSE tap, supervisor wiring) and CORS. If you scope to just
"protocol broker logic" in `core/core.py` (handle_register, handle_invoke,
handle_respond, handle_stream, _route_invocation, plus their helpers), it is
~350 LOC. The NATS replacement of *that subset* is effectively zero — it
becomes the `compile_nats_config` function (~50 LOC) plus the SDK.

---

## 4. Performance: 100 invokes, p50 / p95

`bench.py` runs the dashboard against a real echo node on each stack and
times every `invoke()` from caller-publish to caller-receive.

| Path | n | p50 | p95 | p99 | mean | max |
| --- | --- | --- | --- | --- | --- | --- |
| **mesh-direct-HTTP** (aiohttp + SSE + HMAC) | 100 | 0.674 ms | 1.621 ms | 3.218 ms | 0.902 ms | 6.673 ms |
| **mesh-on-NATS** (nats-py + JetStream audit) | 100 | 0.586 ms | 1.058 ms | 1.894 ms | 0.663 ms | 2.542 ms |

Loopback, M-series Mac, single process for the broker on each side. NATS is
~13% faster at p50 and ~35% faster at p95. The tail is the more interesting
number: NATS' p99 is ~40% lower because the HTTP path has SSE buffer and
aiohttp event loop scheduling adding jitter. Neither is anywhere near a real
bottleneck for this workload.

**Honest read:** at this scale the perf delta does not matter. Both are
sub-millisecond on loopback. The NATS win shows up under fan-out (100
subscribers to one subject) and under cross-host topologies (NATS clusters
hide the network from the SDK). The mesh has no story for either today.

---

## 5. Verdict: should we pivot?

**No — and yes.** Two separate questions are tangled together:

1. **Is NATS a better transport than aiohttp+SSE?** Yes, almost certainly,
   for the reasons in §3 (reconnect, fan-out, replay, ACL, multi-host).
2. **Is NATS a better mesh than the mesh?** No. The mesh is *not* its
   transport. It is:
   - A **manifest** that names every node, every surface, every schema, and
     every allow-edge in one file you can `cat`.
   - A **schema-typed surface model** (`tool` / `inbox` / `approval`,
     `request_response` / `fire_and_forget`).
   - A **declarative ACL** (`relationships:`) that is the same artifact that
     drives discovery, validation, and (in this experiment) the broker config.
   - A **supervisor** that owns process lifecycle from the same manifest.

NATS does *none* of those. It gives you subjects and permissions; you still
have to design what subjects mean, what schemas live where, what edges
exist, and how nodes get spawned. **Replacing the mesh with NATS would
delete the broker and keep all of the hard parts.**

The honest size comparison is *not* "1164 LOC vs 511 LOC". It is "1164 LOC
of mesh vs 511 LOC of mesh-on-NATS **plus** nats-server **plus** the same
manifest **plus** the same schemas **plus** a regenerated nats.conf". The
config compiler (`compile_nats_config`) is doing real work that used to be
implicit in `_route_invocation`.

**Things the mesh has that NATS does not give you, and the next mesh
shouldn't lose:**

- Manifest as the *typed* source of truth. `surfaces[i].schema` is a path to
  a real JSON Schema. `relationships:` is checked at load time. NATS has no
  opinion on any of that.
- Schema validation at the routing layer. The mesh denies a malformed
  payload uniformly; with NATS, every responder must validate.
- A single observable artifact (`audit.log`) that includes denials *at the
  broker layer*. NATS' equivalent is "JS stream + nats-server logs" — two
  places to look.
- A query-able introspection endpoint (`/v0/admin/state`) that shows the
  full graph including schemas. With NATS, that graph lives in the manifest
  file, and the broker has no view of it.

---

## 6. Hybrid option (the recommended path)

Keep the mesh's surface area exactly as-is and use NATS as the **transport
under the SDK** when peer-federating across hosts. Concretely:

- **Single host, dev**: `node_sdk.MeshNode(core_url=...)` over HTTP+SSE.
  Same as today. Zero ops.
- **Multi-host, prod / federation**: `node_sdk.MeshNode(transport="nats",
  url="nats://...")`. The SDK speaks NATS subjects of the form
  `mesh.<from>.<to_node>.<surface>`; a `nats_core.compile_nats_config`
  derives users/permissions from the manifest; a small **mesh-bridge** process
  on each host:
    - generates the node's NATS credentials from the manifest;
    - owns the schema cache and validates payloads on the *publishing* side
      (catches bad payloads before they hit the wire);
    - publishes the same audit subject the prototype uses, so a single
      JetStream stream observes the whole federation.
- The HTTP core stays in place for admin/state/spawn — the dashboard, the
  supervisor, manifest hot-reload don't move. They become a thin control
  plane that *configures* NATS rather than *being* the broker.

That's the highest-leverage version: keep the artifacts that give the mesh
its identity (manifest, schemas, surfaces, supervisor, dashboard), swap the
runtime data plane for something that already solves the boring problems
(reconnect, fan-out, replay, multi-host).

### Concrete next steps if we choose hybrid

1. Promote `nats_core.compile_nats_config` into a real CLI:
   `mesh-config build --manifest manifest.yaml --out nats.conf`. ~80 LOC.
2. Add a `transport: nats` option to `node_sdk.MeshNode` that uses the same
   `mesh.<from>.<to_node>.<surface>` subject convention from this prototype.
   ~150 LOC, all in the SDK.
3. Keep `core/core.py` as the control plane: it loads the manifest, runs the
   supervisor, exposes `/v0/admin/*` for the dashboard, and (in NATS mode)
   owns regenerating `nats.conf` and `SIGHUP`-ing nats-server on manifest
   changes. Drop `_route_invocation`, `handle_invoke`, `handle_respond`,
   `handle_stream`. Saves ~350 LOC.
4. Audit moves to a JetStream stream by default in NATS mode. The existing
   `audit.log` becomes a tail subscriber so on-disk shape doesn't change.
5. Schema validation moves into the SDK (publisher-side and responder-side),
   so cross-language nodes have a clear contract: "if you don't validate,
   the responder will reject."

### What we explicitly should not do

- Do not replace the manifest with raw NATS config. The manifest is the
  product; nats.conf is an implementation detail.
- Do not let nodes publish to arbitrary subjects. The compile step is the
  whole point — one manifest edge, one publish-permission entry.
- Do not let surfaces exist that are not declared in the manifest. The SDK
  should refuse to subscribe to anything outside `surfaces:`.

---

## 7. Repro

```bash
brew install nats-server
cd experiments/nats_pivot
python3 -m venv .venv
.venv/bin/pip install nats-py jsonschema pyyaml aiohttp

# end-to-end demo (3 nodes, schema deny, ACL deny)
.venv/bin/python run_demo.py

# perf bench (HTTP path uses repo's core/core.py)
.venv/bin/python bench.py
cat run_logs/bench.json
```

Logs and the generated `nats.conf` end up in `run_logs/`.

---

## 8. One-line verdict

The mesh's value is the manifest, not the broker — so adopt NATS as the
transport, keep the manifest as the source of truth, and treat the existing
`core/core.py` HTTP path as the local-dev fast path you don't need to delete.
