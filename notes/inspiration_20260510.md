# RAVEN_MESH — Inspiration Scout Report (2026-05-10)

Twenty projects, papers, and protocols deeply analyzed for what RAVEN_MESH should
steal, ignore, or study. Organized into four slices:

1. Multi-agent frameworks & agent-to-agent protocols (MCP, A2A, LangGraph, Swarm, ANP)
2. Service meshes, virtual actors, workflow engines (NATS, Dapr, Temporal, Orleans, libp2p)
3. Robotics middleware & BEAM systems (ROS 2/SROS2, DDS, Phoenix, EMQX, Partisan)
4. Object capabilities, Plan 9, wildcards (Goblins/OCapN, seL4, 9P, Macaroons, Willow/Meadowcap)

Synthesis (top three to study, biggest design blind spot, cross-pollination idea)
is at the end.

---

## Slice 1: Multi-Agent Frameworks & Agent Protocols

### 1. Anthropic Model Context Protocol (MCP)
**URL:** https://modelcontextprotocol.io/specification/2025-06-18 | https://github.com/modelcontextprotocol

**What it is:** MCP is an open protocol that standardizes how LLM applications ("hosts") connect to external tools and data sources via "servers" exposing **Resources**, **Prompts**, and **Tools**. It uses JSON-RPC 2.0 over stdio or "Streamable HTTP" (a single endpoint that can return either `application/json` or upgrade to a `text/event-stream` SSE stream). It is explicitly modeled after the Language Server Protocol — a tiny, transport-agnostic envelope that a whole ecosystem of vendors implements ([overview](https://modelcontextprotocol.io/specification/2025-06-18)).

**Three things RAVEN_MESH could steal:**
1. **The `_meta` reserved namespace convention** ([basic spec, "General fields"](https://modelcontextprotocol.io/specification/2025-06-18/basic)). MCP reserves `_meta` on every message for non-protocol metadata, with a strict prefix rule (`mcp.dev/`, `modelcontextprotocol.io/` reserved). RAVEN_MESH envelopes should bake in a `_meta` slot now — it's the cleanest forward-compat hook for HMAC nonces, trace IDs, and audit tags without polluting the typed surface payload.
2. **`Mcp-Session-Id` header + `Last-Event-ID` resumability** ([transports section](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#session-management)). Sessions are issued on init, sent as a header on every subsequent request, and SSE streams use HTML5 `Last-Event-ID` for replay-on-reconnect. RAVEN_MESH's SSE channel should adopt the per-stream cursor model verbatim — it's stateless on the Core's part except for a tiny replay buffer.
3. **The DNS-rebinding security warning, baked into the spec** ([transports, "Security Warning"](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#security-warning)). MCP requires servers to validate `Origin` and bind to 127.0.0.1 by default. RAVEN's Core, since it routes by surface ID, should ship with the same hard default — local nodes never bind 0.0.0.0.

**One thing NOT to steal:** **Capability negotiation in `initialize`** ([basic protocol](https://modelcontextprotocol.io/specification/2025-06-18/basic)). MCP has a stateful per-connection handshake declaring "my server has tools, sampling, roots…" Don't do this. RAVEN's edges-as-policy means capabilities are *static, declared in the manifest* — you don't negotiate them per connection, you *read* them from the YAML. Putting negotiation in Core leaks "what surfaces exist" knowledge into the runtime, violating "Core does nothing but route."

---

### 2. Google A2A (Agent2Agent) Protocol
**URL:** https://github.com/a2aproject/A2A | https://a2a-protocol.org/latest/specification/

**What it is:** A2A is Google's open protocol for *opaque* agent-to-agent interop, complementary to MCP (MCP = agent↔tools, A2A = agent↔agent). It standardizes JSON-RPC 2.0 over HTTPS (with an HTTP+JSON/REST binding and a gRPC binding) for sending **Messages** that drive multi-state **Tasks** (`SUBMITTED → WORKING → INPUT_REQUIRED/AUTH_REQUIRED → COMPLETED/FAILED/CANCELED/REJECTED`). Discovery is via an `AgentCard` published at a well-known URL ([agent-discovery topic](https://a2a-protocol.org/latest/topics/agent-discovery/)).

**Three things RAVEN_MESH could steal:**
1. **The `/.well-known/agent-card.json` discovery convention** ([agent-discovery](https://a2a-protocol.org/latest/topics/agent-discovery/)). Each agent serves a JSON "business card" at an RFC-8615 path with `name`, `provider`, `serviceEndpoint`, `capabilities.{streaming,pushNotifications}`, `securitySchemes`, and `skills[]`. RAVEN_MESH should expose a `/.well-known/raven-node.json` per-node — even if the manifest is the source of truth, this lets non-mesh tooling sniff a node's surfaces.
2. **Explicit Task lifecycle states with `INPUT_REQUIRED` and `AUTH_REQUIRED` as first-class interrupted states** ([spec §4.1.3 TaskState](https://a2a-protocol.org/latest/specification/)). Most protocols pretend everything is sync or fire-and-forget. A2A bakes in "I need a human / I need a token" as protocol-level states. RAVEN's "approvals are a node" still benefits from these as standardized envelope status codes — every long-running surface should be able to return `INPUT_REQUIRED` instead of inventing per-tool stall semantics.
3. **`Part`-typed message bodies** ([spec §4.1.6](https://a2a-protocol.org/latest/specification/)): a Message contains an array of `parts`, each exactly one of `text | raw | url | data`, with `mediaType` and `filename`. This is far cleaner than MCP's flatter content model and lets RAVEN_MESH reuse a single envelope for chat, file refs, and structured JSON without per-surface schema gymnastics.

**One thing NOT to steal:** **The full HTTP+REST binding with 11 distinct endpoints** (`POST /tasks`, `GET /tasks/{id}`, `:cancel`, `:subscribe`, `pushNotificationConfigs/{id}`, etc., per [spec §11](https://a2a-protocol.org/latest/specification/)). RAVEN routes by *surface id* over a single SSE Core endpoint — proliferating REST routes is exactly the host:port-thinking RAVEN rejects. Push notification config CRUD especially belongs in a "notification node," not Core.

---

### 3. LangGraph
**URL:** https://github.com/langchain-ai/langgraph | https://langchain-ai.github.io/langgraph/

**What it is:** A "low-level orchestration framework for building, managing, and deploying long-running, stateful agents" built around a `StateGraph` whose nodes read/write a shared typed state with per-key **reducers**, executed in Pregel-inspired supersteps. It draws its public API from NetworkX and its execution model from Google Pregel + Apache Beam. First-class durable execution via pluggable **checkpointers** lets graphs survive crashes and resume mid-run.

**Three things RAVEN_MESH could steal:**
1. **Per-key reducers on shared state** ([state.py StateGraph definition](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py)). LangGraph lets each state key declare an aggregation function (e.g. `Annotated[list, add]`) so concurrent node writes merge deterministically. RAVEN_MESH inboxes today are append-only; defining a reducer per surface (concat, last-write-wins, set-union, custom) gives multi-writer surfaces well-defined merge semantics without coordination in Core.
2. **`add_conditional_edges(source, path, path_map)`** ([state.py signatures](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py)) — routing decisions are *functions on state*, not hardcoded. RAVEN's edges-as-policy YAML could grow a tiny optional predicate (`when:` clause referencing envelope fields) modeled exactly on `path_map: dict[Hashable, str]` — keeps Core dumb because the predicate is a pure function evaluated at routing time.
3. **The checkpointer abstraction (`BaseCheckpointSaver` + `thread_id`)** — durable execution is a *plugin*, not a Core feature. Memory checkpointer for tests, Postgres for prod, all behind one interface. RAVEN should adopt the same pattern: a `CheckpointSaver` *node* that the Core writes envelopes to, never a Core feature. This is exactly the "durability is a node" instantiation of RAVEN's principle.

**One thing NOT to steal:** **The `add_node(..., retry_policy, cache_policy, error_handler, timeout)` kitchen-sink signature** ([state.py](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py)). LangGraph absorbs retries, caching, and error handling into the framework. For RAVEN, retries belong in a retry-node, caching in a cache-node. The moment Core knows what "retry" means, the 430-line invariant is dead.

---

### 4. OpenAI Swarm
**URL:** https://github.com/openai/swarm

**What it is:** Swarm is OpenAI's intentionally minimal, educational multi-agent framework built on two primitives: an `Agent` (instructions + tools) and a **handoff** (a tool that returns another `Agent` object, which the runtime detects and switches the active agent). It is stateless across `client.run()` calls (like raw Chat Completions) and threads `context_variables` through every tool call. OpenAI now points production users to the Agents SDK, but Swarm's design is cleaner for a "what's the smallest viable handoff protocol" study.

**Three things RAVEN_MESH could steal:**
1. **Handoff = tool call returning a typed routing object** ([core.py `handle_function_result`](https://github.com/openai/swarm/blob/main/swarm/core.py)): `case Agent() as agent: return Result(value=..., agent=agent)`. The transfer is an ordinary tool-call return, not a special protocol verb. RAVEN_MESH should similarly let any node "hand off" by emitting an envelope whose body is a `Result` containing the next surface id — no new protocol primitive needed, just a typed return.
2. **Implicit context-variable injection by parameter name** ([core.py](https://github.com/openai/swarm/blob/main/swarm/core.py)): `if __CTX_VARS_NAME__ in func.__code__.co_varnames: args[__CTX_VARS_NAME__] = context_variables`. Any tool whose signature declares the magic var receives the shared context dict. RAVEN can do the same for trace IDs, caller node id, and signed-envelope claims — no per-tool boilerplate, opt-in by parameter name.
3. **Stateless `client.run()` loop returning a fresh `Response`** — no hidden session, no server-side conversation. The whole loop is a pure function of (agent, messages, context_variables). RAVEN's Core should preserve this shape: `route(envelope) → envelope` is pure, all durability lives in nodes.

**One thing NOT to steal:** **Sequential in-process tool execution inside the framework's `run()` loop**. Swarm executes all tool calls *inline*, single-threaded, blocking. RAVEN_MESH must route to *out-of-process* nodes by surface id with backpressure-tolerant SSE — copying Swarm's inline loop would re-couple "agent runtime" and "transport," exactly the conflation RAVEN's Core-vs-nodes split exists to prevent.

---

### 5. Agent Network Protocol (ANP)
**URL:** https://github.com/agent-network-protocol/AgentNetworkProtocol | Whitepaper: https://arxiv.org/html/2508.00007v1

**What it is:** ANP is an open protocol positioning itself as "the HTTP of the Agentic Web," organized as **three layers**: (1) Identity & Encrypted Communication based on W3C DID (with a custom `did:wba` Web-Based Agent method that uses HTTPS+DNS instead of blockchain, [spec doc 03](https://github.com/agent-network-protocol/AgentNetworkProtocol)); (2) a **Meta-Protocol Layer** where two agents *negotiate which protocol to speak* before exchanging payloads ([spec doc 06](https://github.com/agent-network-protocol/AgentNetworkProtocol)); (3) Application Protocols for description, discovery, and end-to-end IM ([spec docs 07–09](https://github.com/agent-network-protocol/AgentNetworkProtocol)).

**Three things RAVEN_MESH could steal:**
1. **`did:wba` as the identity scheme** ([did-wba design spec](https://github.com/agent-network-protocol/AgentNetworkProtocol)). Web-Based Agent DIDs derive identity from an HTTPS-served key document — no blockchain, no central registrar. RAVEN's HMAC-signed envelopes today use shared secrets; `did:wba` gives a public-key path forward where any node's identity is just a URL serving a key document, perfectly matching RAVEN's "everything is a node" + manifest approach.
2. **Strict layer separation: identity is *underneath* the protocol, not in it** (whitepaper §2). ANP signs/encrypts at layer 1; layer 2/3 don't see crypto. RAVEN's Core already does HMAC validation at the envelope edge — formalize this as "layer 1," and surface schemas/routing become "layer 3" with no auth cross-talk. The discipline pays off when you migrate to BEAM and want to keep the crypto module isolated.
3. **Capability-described agents discoverable via semantic-web descriptors** ([Agent Description Protocol spec, doc 07](https://github.com/agent-network-protocol/AgentNetworkProtocol)). ANP uses JSON-LD-style descriptors so an unknown agent can be parsed by a stranger. RAVEN's manifest could grow an optional JSON-LD `@context` so external tools (not on the mesh) can read a node's surfaces without RAVEN-specific code.

**One thing NOT to steal:** **The Meta-Protocol negotiation layer itself** ([Meta-Protocol spec, doc 06](https://github.com/agent-network-protocol/AgentNetworkProtocol)). ANP wants two agents to dynamically negotiate "do we speak protocol X v1.2 or Y v0.3?" at connection time. For RAVEN, this is an anti-pattern: the YAML manifest *is* the negotiation, decided at deploy time, not runtime. Pulling negotiation into the live path means Core has to know about protocol versions, schema dialects, and fallback ladders — exactly the "Core does something" violation. Keep negotiation static and out-of-band.

---

## Slice 2: Service Meshes, Actors, Workflow Engines

### 6. NATS (Core + JetStream)
**URL:** https://nats.io / https://github.com/nats-io/nats-server

**What it is:** NATS is a high-performance, simple-by-design pub/sub messaging system written in Go. The core is a fan-out broker addressing messages by hierarchical "subjects" (e.g. `time.us.east.atlanta`); JetStream layers durable streams + consumers on top while keeping subjects as the only address. It is famously small (single binary, ~MB) and is the closest existing thing to RAVEN's "envelope routed by surface id, never host:port" thesis.

**Three things to steal:**
1. **Subject hierarchy as the *only* address.** Subjects are dot-delimited tokens with `*` (single token) and `>` (tail) wildcards; "Messages will be automatically routed to all interested subscribers, independent of location" ([NATS subjects docs](https://docs.nats.io/nats-concepts/subjects)). RAVEN_MESH should adopt this verbatim for surface IDs (e.g. `agent.scheduler.cron.*`, `tool.fs.read`) so allow-edges in YAML can use prefix patterns instead of enumerating endpoints.
2. **Auth callouts as a delegated identity surface.** NATS hands a JWT-encoded auth request to an *external* service which returns a JWT of user claims, with one-time XKey (x25519) keypairs per connection to prevent replay ([Auth Callout docs](https://docs.nats.io/running-a-nats-service/configuration/securing_nats/auth_callout)). RAVEN's Core can keep HMAC envelope signing minimal and push policy/IAM to a node — exactly the "core does nothing" stance.
3. **Stream / Consumer split.** "In JetStream the configuration for storing messages is defined separately from how they are consumed" ([JetStream docs](https://docs.nats.io/nats-concepts/jetstream)). Inboxes (storage) and inbox readers (consumers, with cursor + ack policy) should be different node types in RAVEN, not bolted into Core.

**Don't steal:** The clustered RAFT-replicated stream storage with five-replica linearizable writes. JetStream pulled the broker into the durability business; for RAVEN this would balloon the ~430-line Core into a database. Persistence should be its own node behind a surface, not a Core capability.

**Citation:** [docs.nats.io/nats-concepts/subjects](https://docs.nats.io/nats-concepts/subjects), [auth_callout](https://docs.nats.io/running-a-nats-service/configuration/securing_nats/auth_callout), [jetstream](https://docs.nats.io/nats-concepts/jetstream).

---

### 7. Dapr
**URL:** https://dapr.io / https://github.com/dapr/dapr

**What it is:** Dapr is a CNCF "distributed application runtime" deployed as a sidecar (one Dapr process per app instance). Apps speak HTTP/gRPC to their local sidecar; sidecars speak to each other. It exposes building blocks — service invocation, state, pub/sub, bindings, virtual actors — as uniform APIs decoupled from concrete backends. Service discovery is by `app-id`, not host:port.

**Three things to steal:**
1. **`app-id`-based invocation, not host:port.** "Each application communicates with its own instance of Dapr. The Dapr instances discover and communicate with each other," with calls round-robined across instances of an app-id ([service-invocation-overview](https://docs.dapr.io/developing-applications/building-blocks/service-invocation/service-invocation-overview/)). This is exactly RAVEN's "envelopes routed by surface id" — the sidecar pattern even gives a clean upgrade path for letting non-Python nodes participate.
2. **Turn-based actor concurrency + Placement service.** Dapr actors use "a simple turn-based access model" — one message at a time per actor, no locks — and a Placement service that maps `(actorType, actorId)` to a host ([actors-overview](https://docs.dapr.io/developing-applications/building-blocks/actors/actors-overview/)). For RAVEN, modeling stateful nodes (a chat session, a goal-tracker) as turn-based is a way to get serializability without putting locks in Core.
3. **Bindings as a uniform input/output surface.** Dapr's input bindings (Kafka, cron, MQTT, webhooks) all surface to the app as the same HTTP callback. RAVEN should model timers, webhooks, iMessage receivers identically — every external trigger is a node with a typed outbound surface; Core never grows a "binding" concept.

**Don't steal:** The full sidecar-per-process model with mTLS via the Sentry CA and automatic cert rollover. Sentry alone is thousands of lines of Go; HMAC-signed envelopes are sufficient for an owner-only mesh, and per-process sidecars would multiply RAVEN's footprint. Take the *naming* (app-id), skip the *infrastructure*.

**Citation:** [actors-overview](https://docs.dapr.io/developing-applications/building-blocks/actors/actors-overview/), [service-invocation-overview](https://docs.dapr.io/developing-applications/building-blocks/service-invocation/service-invocation-overview/).

---

### 8. Temporal
**URL:** https://temporal.io / https://github.com/temporalio/temporal

**What it is:** Temporal is a workflow orchestrator where workflow code is durable: it runs as deterministic code that gets re-executed from an Event History on crash/restart. "Temporal doesn't restore memory from a snapshot. It starts the Workflow code from the beginning, replays the Event History step by step" ([docs.temporal.io/workflows](https://docs.temporal.io/workflows)). External code interacts with running workflows via three distinct message types.

**Three things to steal:**
1. **The Signal / Query / Update trichotomy.** Per [encyclopedia/workflow-message-passing](https://docs.temporal.io/encyclopedia/workflow-message-passing): Queries are read-only and never appear in history; Signals are async writes you can't await; Updates are synchronous tracked writes with a response. RAVEN_MESH envelopes today are one shape — splitting the type tag into `query | signal | update` (and validating each against the surface's JSON-Schema accordingly) gives huge clarity for free, and lets nodes opt in to read-only handlers without going through the audit log.
2. **Event-history-as-source-of-truth.** Workflow state *is* the replayable log, not a snapshot. RAVEN's audit log is currently a side-effect; promoting it to a first-class node type whose surface is "give me the log slice for envelope-id X" means memory/scheduling/approval nodes can rebuild themselves by replay. Core stays dumb; durability is a peer.
3. **Determinism boundary = "activities".** Anything non-deterministic (HTTP calls, LLMs, time) must run in an Activity, not the workflow ([docs.temporal.io/workflows](https://docs.temporal.io/workflows)). This is the cleanest articulation of "pure orchestration vs. side-effecting tools" — RAVEN should formalize that orchestrator-nodes only emit envelopes; effecting nodes (tools/devices) own all I/O.

**Don't steal:** Mandatory deterministic replay of all workflow code. Determinism enforcement requires a sandboxed runtime, version-pinned SDKs, and "non-determinism errors" at replay; that's the polar opposite of "Core does nothing." Adopt the *vocabulary* (signal/query/update, activity), not the *mechanism*.

**Citation:** [docs.temporal.io/workflows](https://docs.temporal.io/workflows), [encyclopedia/workflow-message-passing](https://docs.temporal.io/encyclopedia/workflow-message-passing).

---

### 9. Microsoft Orleans (virtual actors)
**URL:** https://github.com/dotnet/orleans / https://learn.microsoft.com/en-us/dotnet/orleans/overview

**What it is:** Orleans coined the "virtual actor" pattern. Grains (actors) are addressed by a stable user-defined key + interface — "actors are purely logical entities that always exist, virtually. An actor cannot be explicitly created nor destroyed... Since actors always exist, they are always addressable" ([Orleans overview](https://learn.microsoft.com/en-us/dotnet/orleans/overview)). The runtime activates them on demand on some silo, deactivates them under memory pressure, and reactivates them anywhere on next call.

**Three things to steal:**
1. **Virtual addressability — call before existence.** Callers send to `(grainType, key)` without knowing/caring whether the grain is loaded, on which silo, or has ever run before. For RAVEN, this means a relationship can target a surface ID like `agent.goal-tracker.fitness-cut` and Core routes it; whether that node is currently a hot Python coroutine, a serialized blob on disk, or has never existed is the *node*'s problem, not Core's. This is the deepest match to RAVEN's philosophy and worth copying explicitly.
2. **Grain placement strategies as pluggable policy.** "The placement process in Orleans is fully configurable" — random, prefer-local, resource-optimized, or custom ([Grain placement](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-placement)). RAVEN doesn't have multi-host yet, but framing "where does this surface live?" as a policy a *placement node* answers (rather than Core's job) keeps the door open for Elixir/BEAM later without rewriting Core.
3. **Stateless workers as a marker.** "Stateless workers are specially marked grains without associated state that can activate on multiple silos simultaneously" ([overview](https://learn.microsoft.com/en-us/dotnet/orleans/overview)). RAVEN should mark pure tool-nodes (e.g. `tool.json.validate`) the same way in the YAML manifest — Core can free-fan-out to any instance instead of routing to a single owner.

**Don't steal:** Grain timers, reminders, and transactional state all baked into the runtime ("ACID transactions... distributed and decentralized... with serializable isolation"). That's enormous surface area inside the host. RAVEN's equivalents (scheduling, persistence, approvals) are explicitly *separate nodes* — keep them that way; let Orleans-style features emerge as composed nodes, not Core features.

**Citation:** [learn.microsoft.com/en-us/dotnet/orleans/overview](https://learn.microsoft.com/en-us/dotnet/orleans/overview).

---

### 10. libp2p
**URL:** https://libp2p.io / https://github.com/libp2p/specs

**What it is:** libp2p is the modular networking stack extracted from IPFS. It separates *who* you're talking to (`PeerId`, derived from a public key) from *how* you reach them (`/ip4/1.2.3.4/tcp/4001/ws` — a `multiaddr`) from *what protocol* you're speaking (`/raven/envelope/1.0.0`, negotiated via multistream-select). Every layer is swappable; the address is content-addressed cryptographic identity.

**Three things to steal:**
1. **PeerId = hash of public key.** Per the [PeerId spec](https://github.com/libp2p/specs/blob/master/peer-ids/peer-ids.md), PeerIds are multihashes of the serialized public key (identity-hash if ≤42 bytes, SHA256 otherwise), supporting Ed25519/RSA/Secp256k1/ECDSA. RAVEN currently HMACs envelopes with a shared secret; promoting node identity to "PeerId = hash(pubkey)" makes manifests self-verifying — the YAML allow-edge `agent.foo -> tool.bar` can carry the recipient's PeerId, and Core verifies signatures without a key registry.
2. **Multiaddr: self-describing, layered addresses.** A multiaddr like `/ip4/.../tcp/4001/p2p/Qm...` carries every protocol layer in one string. RAVEN should adopt the same shape for *transport* descriptors on nodes (`/local/aiohttp/sse/surface/agent.scheduler`) so an Elixir port later just adds new transport tokens without touching surface IDs.
3. **multistream-select for protocol negotiation.** The dialer offers a protocol id like `/raven/envelope/1.0.0`; the listener echoes (accept) or returns `na` ([connections spec](https://github.com/libp2p/specs/blob/master/connections/README.md)). This is the cleanest pattern for *surface versioning*: a node advertises `surface.fs.read/2` and `surface.fs.read/1`, callers negotiate, Core stays dumb about version policy. Beats embedding a version field in the envelope.

**Don't steal:** The full DHT / circuit-relay / NAT-traversal stack. libp2p's value for RAVEN is the *naming and negotiation primitives* — adopting Kademlia or AutoNAT would inject a P2P discovery system into a Core whose entire premise is a single owner running a manifest. Identity & address shapes: yes. Peer discovery infrastructure: absolutely not.

**Citation:** [libp2p/specs PeerId](https://github.com/libp2p/specs/blob/master/peer-ids/peer-ids.md), [connections spec](https://github.com/libp2p/specs/blob/master/connections/README.md).

---

**Slice-2 cross-cut:** Every winner here separates **addressing (the name)** from **transport (the wire)** from **policy (placement, auth, durability)**. NATS does it with subjects, Dapr with app-ids, Orleans with grain keys, libp2p with PeerId+multiaddr, Temporal with workflow-id+signal-name. RAVEN's "surface id" is the same primitive — and the four "don't steal" notes converge on one rule: Core owns the *name-to-route* mapping and signature check; everything else (durability, IAM, placement, retries, transactions) ships as nodes.

---

## Slice 3: Robotics Middleware & BEAM Systems

### 11. ROS 2 (with SROS2)
**URL:** https://docs.ros.org/en/rolling/ • https://github.com/ros2/sros2

**What it is:** ROS 2 is the second-generation Robot Operating System: a distributed framework where every process is a "node" exposing typed message interfaces over four primitives — topics (pub/sub), services (request/reply), actions (long-running goals with feedback), and parameters. The wire layer is abstracted behind RMW (ROS Middleware), with DDS as the default. SROS2 layers PKI-based authentication and per-node access control on top via signed XML "permissions" and "governance" documents stored in an enclave directory tree (cert.pem, key.pem, identity_ca, permissions_ca, governance.p7s, permissions.p7s) ([docs.ros.org SROS2 design](https://design.ros2.org/articles/ros2_dds_security.html)).

**Three things RAVEN_MESH should steal:**
1. **The four-primitive taxonomy (topic / service / action / parameter)** — RAVEN already has "surfaces"; explicitly typing them as one of {fire-and-forget, request-reply, long-running-with-progress, config-read} maps cleanly to existing AI tool-use semantics and gives the Core sane defaults for retries and timeouts per kind.
2. **SROS2's enclave directory layout** — one folder per principal containing cert.pem + key.pem + a signed permissions doc — is a directly portable model. RAVEN's HMAC-signed envelopes could become "enclaves" on disk: `keystore/enclaves/<node_id>/{key,cert,permissions.yaml.sig}` with a permissions CA signing the YAML manifest, replacing trust-on-first-write ([SROS2 design §Keystore Layout](https://design.ros2.org/articles/ros2_dds_security.html)).
3. **The XML access policy with `<topics publish="ALLOW" subscribe="ALLOW">` and "deny rules first"** — RAVEN's manifest can adopt the same shape: per-node `<surface call="ALLOW">` with explicit deny precedence and glob patterns like `/cmd/*` ([ROS 2 access policies](https://design.ros2.org/articles/ros2_access_control_policies.html)).

**Don't steal:** RMW + multi-DDS-vendor abstraction. ROS 2 spends huge engineering effort keeping Fast DDS, Cyclone, Connext, and Zenoh interchangeable. RAVEN's small core can pick *one* transport (HTTP+SSE today, BEAM later) and refuse the abstraction tax.

**Citation:** https://design.ros2.org/articles/ros2_dds_security.html (Keystore Layout section); https://design.ros2.org/articles/ros2_access_control_policies.html (Profile Structure).

---

### 12. DDS (Data Distribution Service) — Fast DDS reference
**URL:** https://fast-dds.docs.eprosima.com/en/latest/fastdds/dds_layer/core/policy/standardQosPolicies.html • https://www.omg.org/spec/DDS-SECURITY/1.1/

**What it is:** DDS is OMG's data-centric pub/sub spec underlying ROS 2. Publishers and subscribers match on Topic + QoS contract; if QoS is incompatible, no data flows. The DDS-Security spec adds five plugin slots (Authentication, Access Control, Cryptographic, Logging, Data Tagging) governed by signed XML documents ([Fast DDS QoS](https://fast-dds.docs.eprosima.com/en/latest/fastdds/dds_layer/core/policy/standardQosPolicies.html)).

**Three things RAVEN_MESH should steal:**
1. **QoS as a per-edge contract, not a global setting.** Each relationship in the YAML manifest could declare `reliability: reliable|best_effort`, `durability: volatile|transient_local`, and `history: keep_last(N)`. `transient_local` is especially powerful: "when a new DataReader joins, its History is filled with past samples" — exactly what an AI agent rejoining a conversation needs (replay last N envelopes) ([Fast DDS Durability §TRANSIENT_LOCAL](https://fast-dds.docs.eprosima.com/en/latest/fastdds/dds_layer/core/policy/standardQosPolicies.html)).
2. **The Partition QoS** — "a logical partition inside the physical partition introduced by a domain. For a DataReader to see changes... they have to share at least one logical partition." This is a free-form string label that participants must share to communicate. Perfect cheap multi-tenancy: RAVEN envelopes carry a `partition` field and Core drops mismatches before schema validation ([Fast DDS Partition QoS](https://fast-dds.docs.eprosima.com/en/latest/fastdds/dds_layer/core/policy/standardQosPolicies.html)).
3. **Five-plugin separation of concerns** (auth / access control / crypto / logging / tagging). Even if RAVEN keeps it as one Core today, naming these as distinct interface points inside `core/agent.py` lets you swap pieces (e.g., HMAC → mTLS) without rewriting routing.

**Don't steal:** RTPS wire format and the IDL type system. DDS's binary, statically-typed, schema-pre-compiled-into-C++ pipeline is brutal overkill for a JSON-Schema, JSON-on-the-wire mesh. RAVEN's 430-line core would balloon 10x.

**Citation:** https://fast-dds.docs.eprosima.com/en/latest/fastdds/dds_layer/core/policy/standardQosPolicies.html (Partition + Durability sections); OMG DDS-Security 1.1 §SPI Plugins.

---

### 13. Phoenix Channels + Phoenix.PubSub
**URL:** https://hexdocs.pm/phoenix/channels.html • https://hexdocs.pm/phoenix_pubsub/Phoenix.PubSub.html

**What it is:** Phoenix Channels are Elixir's WebSocket-based real-time mesh primitive. Clients join string topics like `"room:lobby"` or `"product_updates:7"`; the server's `join/3` callback authorizes per-topic; messages flow via `broadcast!/3` (cluster-wide) or `push/3` (per-socket). Underneath, Phoenix.PubSub abstracts node-to-node fan-out across a configurable adapter (PG2 default, Redis optional, custom via `Phoenix.PubSub.Adapter` behaviour) ([Phoenix Channels guide](https://hexdocs.pm/phoenix/channels.html)).

**Three things RAVEN_MESH should steal:**
1. **The `"topic:subtopic"` convention with `*` wildcard** ("room:*" matches both "room:lobby" and "room:123"). RAVEN's surface addresses become `node:surface[:instance]`, and the manifest grants relationships against patterns like `memory:*` or `agent:scheduler:*`. Cheap, readable, and cleanly maps to existing path-based routing ([Phoenix Channels — Topics](https://hexdocs.pm/phoenix/channels.html)).
2. **The `join/3` authorization callback as the single chokepoint.** Every Channel module decides up front: `def join("room:lobby", _, socket), do: {:ok, socket}` or `{:error, %{reason: "unauthorized"}}`. RAVEN should require every node to implement an explicit `accept_relationship(from, surface)` returning ok/deny, instead of relying on the manifest alone — defense in depth and lets nodes refuse calls during shutdown.
3. **The four-function broadcast API** (`broadcast`, `broadcast_from`, `local_broadcast`, `direct_broadcast(node_name, ...)`). The distinction between cluster-wide, peer-excluding, single-node, and node-targeted is exactly the dispatch matrix RAVEN's Core already implicitly handles — making it explicit prevents accidental loops and matches well with custom dispatcher modules (Phoenix's "fastlaning" pattern for pre-encoded payloads) ([Phoenix.PubSub API](https://hexdocs.pm/phoenix_pubsub/Phoenix.PubSub.html)).

**Don't steal:** Channel "fastlaning" and the full Presence CRDT machinery. Presence's per-process `track/untrack` + diff-broadcasting is gorgeous but assumes thousands of ephemeral processes. RAVEN has dozens of long-lived nodes; a one-line "last_seen" timestamp in the manifest is enough.

**Citation:** https://hexdocs.pm/phoenix/channels.html §"Topics" and §"Authorization"; https://hexdocs.pm/phoenix_pubsub/Phoenix.PubSub.html §"Broadcast Functions".

---

### 14. EMQX (production MQTT broker on BEAM)
**URL:** https://docs.emqx.com/en/emqx/latest/access-control/authz/authz.html • https://docs.emqx.com/en/emqx/latest/extensions/hooks.html

**What it is:** EMQX is a 5-million-connection MQTT broker written in Erlang. Two things make it relevant to RAVEN: a **chained authorization system** (file ACL → built-in DB → Postgres → Redis → HTTP, evaluated in order with `no_match: deny` default) and a **16-hookpoint chain** (`client.connect`, `client.authenticate`, `client.authorize`, `message.publish`, `message.delivered`, etc.) implementing a Chain-of-Responsibility pattern with priority-ordered callbacks ([EMQX authz](https://docs.emqx.com/en/emqx/latest/access-control/authz/authz.html), [EMQX hooks](https://docs.emqx.com/en/emqx/latest/extensions/hooks.html)).

**Three things RAVEN_MESH should steal:**
1. **Authz as an ordered chain with explicit `no_match: deny` fallback.** Each authz source returns allow/deny/no_match; first match wins; if all return no_match, the global default decides. RAVEN's manifest currently is a single source of truth — adding a chain (manifest → per-node policy file → optional HTTP callback) gives operators an upgrade path without rewriting the core ([EMQX authz §Authorization Chain](https://docs.emqx.com/en/emqx/latest/access-control/authz/authz.html)).
2. **Topic placeholders: `home/${username}/data/#`.** RAVEN can let manifest rules reference the calling node's identity: `${node_id}/private/*` is auto-scoped per principal. One rule, N nodes, no template expansion at deploy time ([EMQX authz §Topic Placeholders](https://docs.emqx.com/en/emqx/latest/access-control/authz/authz.html)).
3. **Hook return semantics: `ok | {ok, NewAcc} | stop | {stop, NewAcc}`.** RAVEN already has approval-style middleware; formalizing the four return shapes (pass-through, transform, halt, halt-with-result) gives a clean extension point so a "memory-redact" or "rate-limit" node can be inserted into the message path without modifying Core ([EMQX hooks §Return Value Semantics](https://docs.emqx.com/en/emqx/latest/extensions/hooks.html)).

**Don't steal:** The 16 hookpoints themselves. EMQX needs that granularity because it runs MQTT 5.0; RAVEN has maybe four meaningful events (envelope_received, schema_validated, routed, delivered). Don't import the whole vocabulary — just the chain pattern.

**Citation:** https://docs.emqx.com/en/emqx/latest/access-control/authz/authz.html (chain section); https://docs.emqx.com/en/emqx/latest/extensions/hooks.html (HookPoint Locations + Return Value Semantics).

---

### 15. Partisan (lasp-lang/partisan)
**URL:** https://github.com/lasp-lang/partisan

**What it is:** Partisan is a drop-in alternative to Distributed Erlang that replaces the full-mesh, single-TCP-connection, heartbeat-based topology with **pluggable overlays** (full-mesh, HyParView, client-server, static), **named channels** (multiple parallel TCP connections between the same node pair, segmented by purpose), and **TCP-verified failure detection** instead of `net_tick_time`. Goal: scale BEAM clusters from ~60-200 nodes to thousands while keeping `gen_server` API compatibility.

**Three things RAVEN_MESH should steal (especially as it eyes BEAM):**
1. **Named channels for traffic isolation.** Partisan separates background/maintenance traffic from application messages on different TCP sockets to avoid head-of-line blocking. RAVEN should reserve a `system` channel (heartbeats, manifest updates, audits) distinct from `data` (envelope routing) — even on top of HTTP/SSE today, this is just two SSE streams per node ([Partisan README §Channels]).
2. **"Bring Your Own Overlay."** Partisan lets the topology be configured per deployment: full-mesh for small clusters, HyParView for high-churn. RAVEN's Core-as-hub is fine for ~10 nodes; designing the manifest so node-to-node direct edges are *expressible* (even if currently always routed through Core) means switching to peer-to-peer later doesn't require a rewrite.
3. **TCP-verified failure detection at each gossip round** rather than time-based heartbeats. RAVEN should treat liveness as "did the last envelope ack within T?" not "did this node send a hello in the last 30s?" — fewer false positives, no clock drift issues.

**Don't steal:** Partisan's full overlay-strategy zoo. HyParView, plumtree, etc. are wonderful papers but RAVEN with a dozen nodes will run on full-mesh forever. Just reserve the *interface* for swapping; don't implement four overlays now.

**Citation:** https://github.com/lasp-lang/partisan README §"Channels" and §"Failure Detection"; Meiklejohn et al., "Partisan: Scaling the Distributed Actor Runtime" (USENIX ATC 2019).

---

## Slice 4: Object Capabilities, Plan 9, Wildcards

### 16. Spritely Goblins + OCapN — modern object-capability framework
**URL:** https://spritely.institute/goblins/ • https://ocapn.org/ • https://codeberg.org/spritely/goblins

**What it is:** Goblins is a distributed object programming environment (Guile + Racket) built on the E-lineage of object capabilities. Objects live in **vats** (single-threaded event loops); intra-vat sends use synchronous `$`, cross-vat sends use asynchronous `<-` returning promises. Networking is handled by **CapTP** (Capability Transport Protocol) inside the **OCapN** spec, which adds a netlayer abstraction (Tor onion services, libp2p, etc.) and certificate-based third-party handoffs so two peers can securely share a reference to an object on a third peer (https://spritely.institute/news/introducing-ocapn-interoperable-capabilities-over-the-network.html). Identity is reference-based: "if you don't have it, you can't use it" — the only way to invoke an object is to already hold a reference (the *spritely-core* paper's central thesis).

**Three things RAVEN_MESH could steal:**
1. **Promise pipelining over CapTP.** Instead of `B→A→B→A→B`, you send the next message to a *promise for the result* in a single round-trip (`B→A→B`). Quoted in spritely-core: "the speed of light is constant and New York is not getting any closer to Tokyo." For RAVEN_MESH this means a node can chain `route(call(node_x.tool, args), to=node_y.inbox)` without waiting on Core. (https://files.spritely.institute/papers/spritely-core.html)
2. **Sturdyrefs + swiss numbers as the on-wire identity for surfaces.** A sturdyref is `ocapn://<machine>/<swissnum>` where the swissnum is an unguessable object-specific token. Replace RAVEN_MESH's HMAC-signed envelope+name with `(node_pubkey, surface_swissnum)` — possession of the pair *is* the capability, no manifest lookup needed. (https://files.spritely.institute/docs/guile-goblins/0.16.1/Using-the-CapTP-API.html)
3. **Three-vat handoff certificates.** When Alice wants Bob to talk to Carol, she signs a handoff cert that Bob presents to Carol; Carol verifies without needing Alice online. RAVEN_MESH can use this exact pattern so an agent can grant a *delegate* edge to a tool without round-tripping Core. (OCapN handoff spec, https://github.com/ocapn/ocapn)

**Don't steal:** the **vat = synchronous-island model** with eventual-loop turns. Goblins requires deep host-language integration (Racket/Guile delimited continuations) to make `$` vs `<-` ergonomic. For a Python aiohttp Core, faking vats adds complexity without payoff — keep async-everywhere.

**Citation:** *The Heart of Spritely: Distributed Objects and Capability Security*, sections on Vats, Promise Pipelining, Sealers & Unsealers — https://files.spritely.institute/papers/spritely-core.html

---

### 17. seL4 microkernel — capabilities as the only authority
**URL:** https://sel4.systems • https://docs.sel4.systems/projects/sel4/api-doc.html

**What it is:** seL4 is a formally-verified microkernel where *every* kernel-mediated operation requires invoking a **capability** — an unforgeable kernel-held token granting rights to a kernel object (thread, page, endpoint, untyped memory). Each thread owns a **CSpace**: a tree of CNodes (capability tables) addressed by `(cap_ptr, depth)`. New capabilities are minted from existing ones via `seL4_CNode_Mint` / `seL4_Untyped_Retype`, and the **Capability Derivation Tree (CDT)** tracks parent/child so that revoking a parent automatically and atomically revokes all children. IPC is done by invoking endpoint caps; the kernel attaches a **badge** (a machine-word identifier set at mint time) so receivers can authenticate senders without trusting them.

**Three things RAVEN_MESH could steal:**
1. **Badged endpoints for relationship identity.** A "badge" is a 64-bit integer the kernel splices into IPC so the receiver knows which mint of the cap was used. RAVEN_MESH should badge each allow-edge: same surface, different badges per requester ⇒ per-edge audit logs, per-edge rate limits, and per-edge revocation, all without changing the surface. (seL4 manual §4.2 "Endpoint Badges", https://sel4.systems/Info/Docs/seL4-manual-latest.pdf)
2. **Capability Derivation Tree for cascading revocation.** When Core deletes an edge, every edge derived from it (delegations) dies in one step. The current RAVEN_MESH YAML manifest has no parent/child — adding a `derived_from` field gives free recursive revoke. (seL4 manual §3.1.4 "Capability Derivation")
3. **Untyped → Retype as the *only* object-creation path.** No god-mode "create a node" syscall; you must consume a finite untyped capability to mint a node. Map this to RAVEN_MESH: spawning a sub-agent must consume a quota cap, preventing fork-bomb agents.

**Don't steal:** the **physical-memory accounting** model (untyped covers raw RAM). RAVEN_MESH nodes are logical; making operators hand-budget bytes per node would be ceremony with no security gain at the application layer.

**Citation:** *seL4 Reference Manual* §3 "Capability-based Access Control", §4 "Message Passing (IPC)" — https://sel4.systems/Info/Docs/seL4-manual-latest.pdf

---

### 18. Plan 9 / 9P (9P2000) — namespace-as-protocol
**URL:** http://9p.io/sys/doc/9.html • https://ericvh.github.io/9p-rfc/rfc9p2000.html

**What it is:** Plan 9 unifies every resource (devices, processes, networks, GUI windows) behind a single file protocol, **9P**. Clients send **T-messages** (Tversion, Tauth, Tattach, Twalk, Topen, Tread, Twrite, Tclunk, Tremove…), servers reply with **R-messages**. Files are referenced by **fids** (file identifiers, client-allocated handles); `Twalk` traverses one path component at a time and produces a *new* fid for the destination, which `Topen` then locks for I/O. Each *process* has its own **namespace** assembled by `mount`/`bind`, including **union directories** that stack multiple servers at one path (the elegant solution to `$PATH`). Authentication is bootstrapped by `Tauth` which establishes an **afid** — a special fid you read/write to perform whatever auth dance the server defines, then pass to `Tattach`.

**Three things RAVEN_MESH could steal:**
1. **`Twalk`-style surface discovery.** Instead of `GET /manifest` returning the entire graph, expose `walk(node, [path components])` that returns a fresh fid per hop. A node can introspect just the slice it's allowed to traverse (edges + sub-surfaces) without seeing the rest. Each walk-step is a permission check, mirroring 9P §"walk" (rfc9p2000 §"walk"). (https://ericvh.github.io/9p-rfc/rfc9p2000.html)
2. **`afid` as a generic auth-handshake surface.** RAVEN_MESH currently bakes HMAC-signed envelopes into Core. 9P factors auth *into a file*: the client opens an afid, exchanges arbitrary bytes (DES tickets, p9any, TLS, anything), then attaches. RAVEN_MESH could expose an `auth` surface per node and let pluggable schemes (HMAC today, DIDComm tomorrow) ride on top.
3. **Union directories / per-node namespaces.** A node's view of the mesh = union of `(its allowed edges) ∪ (its subscriptions) ∪ (built-ins)`, assembled per-process. Two agents on the same Core legitimately see different meshes — this is exactly the "no policy field, edge ⇒ allowed" capability model expressed as a namespace. (Plan 9 paper §"Per-Process Name Spaces")

**Don't steal:** **stateful fids across long-lived connections.** 9P assumes a reliable session; fids leak if a client crashes. Modern aiohttp+SSE is lossy and reconnect-y — keep RAVEN_MESH's stateless message envelopes; only borrow the *naming* discipline, not the session state.

**Citation:** *Plan 9 from Bell Labs* (Pike et al.), §3 "Network Environment / Per-Process Name Spaces", and *9P2000 RFC* (Hensbergen) §"messages" walk/open/auth — http://9p.io/sys/doc/9.html and https://ericvh.github.io/9p-rfc/rfc9p2000.html

---

### 19. Macaroons — bearer credentials with contextual caveats
**URL:** https://research.google/pubs/macaroons-cookies-with-contextual-caveats-for-decentralized-authorization-in-the-cloud/ • paper PDF: https://theory.stanford.edu/~ataly/Papers/macaroons.pdf

**What it is:** Macaroons (Birgisson, Politz, Erlingsson, Taly, Vrable, Lentczner — NDSS 2014) are bearer tokens built from a **chained HMAC**: starting from a root secret `K_R`, you compute `sig_0 = HMAC(K_R, id)` then for each appended caveat `c_i`, `sig_i = HMAC(sig_{i-1}, c_i)`. Anyone holding the macaroon can append a caveat (e.g. `path = /inbox`, `before = 2026-05-11T00:00Z`, `node = node_x`) and the new signature is just `HMAC(prev_sig, new_caveat)` — they cannot widen rights, only narrow them (**attenuation**), because they lack the upstream key. **Third-party caveats** embed a challenge that must be answered by a separate **discharge macaroon** from another authority, enabling decentralized auth (e.g. "valid only if Auth-Service-X also signs that user is logged in"). Verification is the resource owner replaying the HMAC chain with `K_R`.

**Three things RAVEN_MESH could steal:**
1. **Attenuation by caveat-append for delegation.** Today RAVEN_MESH edges are static YAML. Replace/augment with macaroon envelopes: an agent can hand a tool a token saying "you may call `node_x.tool` *only* with `args.budget < 10` *only* until `t+60s`," and the tool need only the root key to verify. No central edits. (paper §3 "Construction", https://theory.stanford.edu/~ataly/Papers/macaroons.pdf)
2. **Third-party caveats for cross-Core federation.** When two RAVEN meshes interoperate, embed `cid = "user is alice@coltonk", vid, location = auth.coltonk.com"`; the receiver demands a discharge macaroon from `auth.coltonk.com`. Decentralized identity without a global PKI. (paper §3.2 "Third-party caveats")
3. **HMAC-chain envelopes as a drop-in upgrade to current single-HMAC envelopes.** RAVEN_MESH already HMAC-signs envelopes. Switching to chained HMAC turns every envelope into an attenuable cap with zero crypto-library upgrade — same primitive. (Stanford PDF §2.2 "HMAC chains", https://theory.stanford.edu/~ataly/Papers/macaroons.pdf)

**Don't steal:** **opaque/freeform caveat strings.** The paper deliberately leaves caveat semantics to the verifier, which fly.io's "Macaroons Escalated Quickly" post (https://fly.io/blog/macaroons-escalated-quickly/) shows is a foot-gun — verifiers diverge on parsing. RAVEN_MESH should pin caveats to its **JSON-Schema surface vocabulary** (the same schemas surfaces already validate) so a caveat is just a schema fragment, not a string.

**Citation:** Birgisson et al., *Macaroons: Cookies with Contextual Caveats for Decentralized Authorization in the Cloud*, NDSS 2014, §3 "Macaroons" (construction), §3.2 "Third-party caveats", §3.3 "Discharge" — https://theory.stanford.edu/~ataly/Papers/macaroons.pdf

---

### 20. Willow Protocol + Meadowcap — path-based capabilities with CRDT sync
**URL:** https://willowprotocol.org/ • https://willowprotocol.org/specs/meadowcap/index.html

**What it is:** Willow is a peer-to-peer key-value sync protocol designed for *destructive* edits (real deletion, not tombstone-only). Data lives in `(namespace, subspace, path, timestamp) → payload` 4-tuples. **Meadowcap** is its capability layer: a capability is a signed token bestowing read or write on a *region* of that 4D space, restrictable along **subspace, path-prefix, and time-range** axes. Two namespace flavors: **communal** (each subspace owned by a pubkey — bottom-up) and **owned** (a single namespace pubkey signs the root cap — top-down). Delegation: Alfie holding cap `C` mints a new cap for Betty by signing `(C, Betty.pubkey, restrictions)`; verification recursively walks the signature chain back to the namespace root. The companion **WGPS** (Willow General Purpose Sync) protocol does set-reconciliation sync over only the regions both peers' caps cover.

**Three things RAVEN_MESH could steal:**
1. **Path-prefix + time-range restrictions as first-class caveats.** Meadowcap caps natively carry `granted_area = {subspace, path_prefix, time_range}`. RAVEN_MESH surfaces are already path-shaped (`node/surface/method`); add `path_prefix` and `expires_at` to every edge and you get fine-grained delegation for free. (Meadowcap spec §"Capabilities", https://willowprotocol.org/specs/meadowcap/index.html)
2. **Communal vs owned namespaces** as a model for *who* creates a sub-mesh. A user's personal mesh is communal (each agent = subspace, owns its keypair); an org's mesh is owned (org's root key signs all delegations). RAVEN_MESH today only supports the owned flavor — adopting communal namespaces lets agents bring their own identity to a federated mesh. (Meadowcap §"Communal namespaces")
3. **Sync only the caps' intersection.** WGPS exchanges the *fingerprint* of each peer's reachable region, then reconciles. For RAVEN_MESH SSE streams: a node should only receive events from edges whose `granted_area` intersects its subscription, computed without leaking the rest. (Willow data model §"Areas of Interest", https://willowprotocol.org/specs/data-model/index.html)

**Don't steal:** **destructive prefix-pruning** (Willow's signature feature: a cap can authorize deleting *all* paths under a prefix). For RAVEN_MESH this is a footgun — a compromised delegation could nuke an agent's entire surface tree. Keep deletes per-edge and append-only-audit, the way the current Core does.

**Citation:** *Meadowcap Specification* §"Communal capabilities", §"Owned capabilities", §"Delegation" — https://willowprotocol.org/specs/meadowcap/index.html ; *Willow Data Model* §"Entries and Areas" — https://willowprotocol.org/specs/data-model/index.html

---

# Synthesis

## If I had to pick 3 projects to study deeply this week

**1. Spritely Goblins + OCapN.** Of everything reviewed, this is the closest existing
project to RAVEN_MESH's actual philosophy: every participant is a logical object,
identity is reference-possession, and the wire protocol (CapTP) is a thin envelope
that can ride on multiple netlayers (Tor, libp2p, plain TLS). Three artifacts
deserve careful reading: (a) the *spritely-core* whitepaper for the conceptual
spine, (b) CapTP's promise pipelining for the round-trip-elimination pattern, and
(c) the OCapN three-party handoff certificate scheme — that one *directly*
answers the question "how do I delegate an edge without round-tripping Core?"

**2. Macaroons (NDSS 2014 paper + fly.io post-mortem).** RAVEN_MESH already HMAC-signs
envelopes. A 50-line change turns each signature into a chained HMAC, and suddenly
the static YAML manifest becomes the *root* grant while runtime envelopes carry
attenuated, time-bounded sub-caps. The fly.io post-mortem is the negative example
worth internalizing: don't leave caveat semantics opaque — bind caveats to the
existing JSON-Schema surface vocabulary so verifiers can't diverge.

**3. Microsoft Orleans (just the docs, ~half a day).** Of the actor systems, Orleans
articulates the "virtual addressability" idea most cleanly, and its placement
strategy + stateless-worker marker map almost one-to-one onto RAVEN_MESH manifest
fields. Reading the Orleans overview gives you the vocabulary to redescribe
RAVEN's surface model in actor-system terms, which will be invaluable when the
BEAM port starts (since BEAM is a virtual-actor system natively).

## The biggest blind spot in current RAVEN_MESH design

**Delegation. The manifest is static, and there is no mechanism for an agent to
narrow and forward a capability at runtime.**

Every other system reviewed has a story for this:

- **Macaroons:** append a caveat, the chain is self-verifying; root key is the
  only secret needed.
- **Meadowcap:** delegation = signing `(parent_cap, new_pubkey, restrictions)`; verification walks the chain.
- **seL4:** mint a new cap from an existing one, attach a badge; revoke via the
  Capability Derivation Tree.
- **Goblins/OCapN:** three-party handoff certificates let Alice grant Bob a path
  to Carol without Carol ever phoning home.
- **NATS auth callouts:** delegate the whole policy check to a separate service.

RAVEN_MESH has none of these. As soon as a capability node ("approval", "kanban
writer") wants to grant a *narrowed* version of its right to a sub-agent — say,
"call kanban_node.create_card, but only on board X, only for the next 5
minutes" — there is no protocol-level path. The operator must edit YAML. That's
fine when there is one user and ten nodes; it breaks the moment an LLM agent
decides at runtime to spawn a sub-agent with reduced authority. **Adopting
Macaroons-style chained HMAC envelopes is the smallest change that closes this
gap, and it preserves the existing single-key infrastructure.**

A close runner-up: **no replay-on-reconnect.** MCP solved this with `Mcp-Session-Id`
+ `Last-Event-ID`; DDS solved it with `transient_local` durability. RAVEN's SSE
stream loses any envelope sent during a reconnection window. This is fine for
the demo; it will be painful in production with a flaky agent process.

## An idea that emerges from combining several of these projects, that nobody seems to have explicitly built

> **A "manifest-rooted, HMAC-chained, schema-typed delegation envelope" — RAVEN_MESH's
> static YAML allow-edges as the *root* of a Macaroon-style capability graph, with
> each runtime envelope carrying an arbitrary chain of attenuating caveats drawn
> from the existing JSON-Schema surface vocabulary, plus Meadowcap-style
> path/time restrictions, plus an OCapN-style three-party handoff cert when a
> delegated edge crosses Core boundaries.**

Concretely:

1. **Root caps are still the YAML manifest.** Each allow-edge `{from: A, to: B.surface}`
   becomes a root-signed macaroon: `sig_0 = HMAC(K_core, "A→B.surface")`.
2. **Attenuation = appending JSON-Schema-typed caveats.** Instead of opaque
   strings (the fly.io footgun), a caveat is a fragment of the surface's existing
   JSON-Schema, e.g. `{"args": {"properties": {"budget": {"maximum": 10}}}}` or
   `{"$expires_at": "2026-05-11T00:00Z"}`. A child agent appends one with
   `sig_n = HMAC(sig_{n-1}, canonical(caveat))` — no new crypto, just one extra
   line of code in `core/agent.py`.
3. **Verification reuses what Core already does.** When the chained envelope
   arrives, Core (a) replays the HMAC chain with the manifest root key, (b) for
   each caveat, validates the *invocation payload* against the merged schema
   (existing schema ∩ all caveat constraints), and (c) routes if every step
   passes. The "edge ⇒ allowed" rule is preserved: the root edge IS the allow.
4. **Path/time bounds from Meadowcap.** Caveats can include `path_prefix`
   ("only surfaces under `kanban.board.work.*`") and `time_range` natively —
   they're just JSON-Schema additions.
5. **Cross-Core handoffs use OCapN-style signed certificates.** When a delegated
   envelope crosses to a federated peer mesh, the peer Core verifies the cert
   (peer-pubkey, granted-area, restrictions) without re-checking with the
   originator. Combined with `did:wba` from ANP, peer identity is a URL.
6. **Revocation = adding a row to a per-Core "revoked-prefix" set.** seL4-style
   cascading revocation falls out for free: revoke any signature in the chain
   and every descendant dies.

The thing nobody has built: **all five of these primitives married to a single,
typed, schema-validated runtime envelope where the schema *is* the caveat
language.** Macaroons leave caveats as freeform strings (footgun). Meadowcap
restricts to subspace/path/time only (less expressive than JSON-Schema).
Goblins/OCapN delegate at the object level but don't have a typed payload
vocabulary. seL4 has badges but no path/time predicates. RAVEN_MESH already has
JSON-Schema-validated payloads as a load-bearing piece of the design — bolting
the macaroon construction onto that is a unique, achievable synthesis. It would
also slot perfectly into the eventual BEAM rewrite, where chained-HMAC
verification is a pure function and trivially distributable.

The one-line summary: **the manifest is the policy, but every envelope is a
runtime attenuation of it.**
