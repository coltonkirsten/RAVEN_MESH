# RAVEN_MESH Capability Graph — Formal Model and Extensions

> Worker note (capability graph track, 2026-05-10). Companion to
> `notes/inspiration_20260510.md` slice 4 (object capabilities, Plan 9,
> Macaroons, Meadowcap) and `notes/synthesis_20260510.md`. Read those first
> for the citations and the "biggest blind spot" framing this paper builds on.

This document does three things:

1. Formalises what RAVEN_MESH does *today* with allow-edges, in Datalog.
2. Compares that model against four mature capability systems (Goblins / OCapN, seL4, Macaroons, Meadowcap) at sufficient granularity to see what mesh actually has and what it does not.
3. Proposes four concrete extensions — each with a YAML manifest fragment that fits the existing schema with additive changes only — that close the gap without abandoning "edge ⇒ allowed" as the runtime invariant.

## 1. What the system does today

A RAVEN_MESH manifest declares:

- A finite set of **nodes**, each with a stable string `id`, a `kind` (`actor | capability | approval | hybrid`), an `identity_secret` (used for HMAC), and a list of **surfaces**.
- Each surface has a `name` (unique within the node), a `type` (`tool | inbox`), an `invocation_mode` (`request_response | fire_and_forget`), and a JSON-Schema reference that defines the shape of the payload it accepts.
- A list of **relationships** — directed edges of the form `(from_node_id, target_node_id.surface_name)`.

At runtime, Core's `_route_invocation` reduces every authorisation question to a single membership test: `(env.from, env.to) ∈ state.edges` (`core/core.py:267`). There is no policy field, no role, no group; the existence of the edge in the manifest *is* the grant.

A capability, in this system, is therefore exactly: **the right of node A to invoke surface S on node B, granted by the presence of the relationship `(A, "B.S")` in the loaded manifest**. The grant arrives by exactly one mechanism: an operator edits the YAML and Core reloads it. Nothing else can change the edge set: nodes cannot vouch for each other, cannot pass capabilities to peers, cannot mint sub-capabilities, and cannot ask Core "what may I invoke?" except by reading the relationships block returned at registration. The smallest unit of authority is the surface — once an edge exists, it authorises every payload the surface's JSON-Schema validates, with no narrower predicate available.

## 2. Formal model (Datalog)

We adopt Datalog as the notation: it is the canonical formalism for capability systems (see seL4's CapDL, Halloran et al.'s policy logics) and it makes the absence of higher-order facts (delegation, attenuation, time) immediately legible — there are no rules about minting or delegating because those base predicates do not exist.

### 2.1 Base predicates

Extracted directly from `core/core.py:108-129` and the manifest schema:

```
node(N, Kind)                       % N is a declared node of kind Kind
surface(N, S, Type, Mode, SchemaId) % node N exposes surface S
allow_edge(A, B, S)                 % manifest: from A to B.S
secret(N, K)                        % N's HMAC secret resolved
schema(SchemaId, Schema)            % JSON-Schema for a surface
connected(N, SessionId)             % N has an open SSE stream
sig_valid(Env, K)                   % HMAC-SHA256 of canonical(Env) matches K
schema_valid(Payload, Schema)       % jsonschema.validate succeeded
```

### 2.2 Derived predicates and the routing rule

The full authorisation predicate that Core implements is:

```
can_invoke(Env) :-
    Env = envelope(_, From, B, S, Payload, Sig),
    node(From, _),                             % core.py:249  unknown_node check
    secret(From, K), sig_valid(Env, K),        % core.py:256  bad_signature
    allow_edge(From, B, S),                    % core.py:267  denied_no_relationship
    surface(B, S, _, _, SchemaId),             % core.py:275  unknown_surface
    schema(SchemaId, Schema),
    schema_valid(Payload, Schema),             % core.py:283  denied_schema_invalid
    connected(B, _).                           % core.py:292  denied_node_unreachable
```

Every `denied_*` audit code is one missing literal in this conjunction. There are no other rules — the relation `can_invoke/1` has no second clause, no transitive closure, no inheritance. In particular, there is **no rule of the shape**

```
% absent today
can_invoke(Env) :- delegated_cap(Env, ParentCap), valid(ParentCap), narrows(Env, ParentCap).
```

That absence is the formal statement of "there is no delegation".

### 2.3 Useful queries (what mesh introspection *could* answer)

Because `allow_edge/3` is fully materialised, several useful queries are one-liners. The demo (`demo.py`) implements them.

```
who_can(B, S, A)       :- allow_edge(A, B, S).         % all callers of a surface
what_can(A, B, S)      :- allow_edge(A, B, S).         % all surfaces a node may call
reachable(A, B)        :- allow_edge(A, B, _).
reachable(A, C)        :- allow_edge(A, B, _), reachable(B, C).
is_path(A, B, S)       :- reachable(A, B), surface(B, S, _, _, _).
```

`reachable/2` is interesting because it is *not* the runtime authorisation question — Core only checks the direct edge — but it is the question an attack-surface auditor wants to ask: "starting from `dummy_actor`, what capabilities can a single chain of legitimate invocations eventually reach?" The demo answers all five.

### 2.4 Reading the actual manifests through this lens

`manifests/full_demo.yaml` defines 7 nodes and 50 allow-edges. A few patterns leap out once you write them as Datalog:

- `approval_node` is a **mediating principal**: the relation `allow_edge(approval_node, _, _)` has 9 outgoing edges (cron, webui, kanban writes), and inbound `allow_edge(_, approval_node, inbox)` has 3 callers (`dummy_actor`, `human_node`, transitively `nexus_agent`). It is the only node whose forwarding semantics are operationally documented: it wraps inbound envelopes in `wrapped` and re-signs (`docs/PROTOCOL.md` §4.2).
- `human_node` is the **god-node**: it has direct edges to every surface every other node exposes. In capability terms this is the "ambient authority" anti-pattern — a single principal whose compromise hands an attacker the entire mesh.
- `cron_node` is a **scheduled-then-static-edge** invoker: edges from `cron_node` to `webui_node.show_message`, `webui_node.change_color`, and `kanban_node.create_card` exist *because* the manifest authors anticipated cron firing those calls. Cron's actual job *list* is dynamic, but its allowed *targets* are frozen.

These observations matter because they show that the static-edge model already encodes design choices (ambient-authority for humans, mediation for approvals) that mature capability systems would express explicitly with delegation and badges.

## 3. Mapping to mature capability systems

Rigour means showing the mapping concretely, not waving at "ocaps".

**Object capabilities (Goblins / OCapN, E-lineage).** In the Mark Miller / Spritely model, a capability is an **unforgeable reference** to an object: holding the reference *is* the right; there is no separate access-control list. RAVEN_MESH does not implement object capabilities — the surface id `"kanban_node.create_card"` is a *guessable name*, not an unforgeable reference, and possession of the name is insufficient (you also need the static edge). The closest thing in mesh today is the `(node_id, secret)` pair, which does function like an unforgeable reference: signing an envelope with `dummy_actor`'s HMAC key is the only way to act *as* `dummy_actor`. So mesh has *node-level* ocaps (the secret is the cap to be that node) but *not* surface-level ocaps. Goblins additionally provides three-party handoffs: a node holding a reference can hand a copy to a peer with attenuating wrappers. Mesh has no analogue — to "hand off" a capability, an operator must edit YAML.

**seL4 microkernel capabilities.** seL4's primitives are the closest formal cousin. seL4 caps live in a CSpace, are minted from existing caps via `seL4_CNode_Mint`, may carry a 64-bit *badge* the receiver sees, and are organised into a Capability Derivation Tree so revoking a parent atomically revokes all descendants. Mesh has none of: minting, badges, or a derivation tree. The manifest is one flat set; revoking the edge `(human_node, kanban_node.delete_card)` revokes exactly that edge and nothing else, because no edge is "derived from" another — there is no tree to walk.

**Macaroons (Birgisson et al., NDSS 2014).** Macaroons are HMAC-chained bearer tokens that attenuate by appending caveats (`HMAC(prev_sig, new_caveat) = new_sig`); the holder cannot widen rights, only narrow them. RAVEN_MESH already HMAC-signs envelopes (`docs/PROTOCOL.md` §2.1) — the cryptographic substrate is half-built. What is missing is *chaining*: today every envelope's HMAC is computed against the sender's root secret, not against a prior signature, so there is no notion of an attenuated, time-bounded, payload-narrowed sub-token a holder can pass around.

**Meadowcap / Willow.** Meadowcap caps carry a `granted_area = (subspace, path_prefix, time_range)` and verification walks a delegation chain back to a root pubkey. Mesh edges have *node-id + surface-name* (analogous to subspace + path) but no `path_prefix` (so there is no way to grant "any kanban surface starting with `read_`"), no `time_range` (so no expiration), and no chain (so no delegation).

A compact one-line summary: **mesh's authority unit is the unattenuated, unbounded, unforwardable, root-issued allow-edge**. Every adjective is one feature short of the corresponding mature system.

## 4. Five concrete weaknesses

These follow from the formal model and the comparison above; each names a class of programs that mesh-as-it-stands cannot express.

1. **No delegation.** When `nexus_agent` decides at runtime to spawn a sub-agent that should be allowed to invoke `kanban_node.create_card` *but only on board X*, there is no protocol-level move that makes this happen. The operator must edit YAML, restart Core, and the new sub-agent must be a manifest-declared node.
2. **No scoped capabilities.** The edge `(human_node, kanban_node.delete_card)` permits deleting *any* card. There is no way to express "delete only cards in the `archive` column" or "delete only cards you created" without rewriting the surface to push those constraints into the schema, which would couple the schema to the principal.
3. **No path or time bounds.** An edge has no expiry. `(approval_node, cron_node.set)` is just as live one year after deploy as it was on day one. Anything resembling lease semantics has to be re-implemented per node (and currently, isn't).
4. **No introspection symmetry.** Core returns a node's relationships at registration (`core/core.py:228`) and exposes `/v0/introspect` for the whole graph (`core/core.py:412`), but there is no first-class `_capabilities` surface a node serves to its peers, no equivalent of A2A's `/.well-known/agent-card.json`, and no walk-style permission-checked traversal the way Plan 9's `Twalk` does it (one hop, one permission check, one fresh fid). A peer cannot ask "what may I invoke?" without a backchannel to Core.
5. **Granularity is per-surface, not per-payload-shape.** Once `(A, B.S)` exists, every payload that satisfies `B.S`'s schema is allowed. There is no way to express "A may call B.S but only with `payload.budget < 10` and `payload.priority != 'high'`". Many surfaces accept whole families of distinguishable operations and the mesh gives operators no tool to slice them.

## 5. Extensions

The four extensions below are designed to be *additive*: existing manifests stay valid; existing nodes need no changes; the routing rule's first clause stays exactly `allow_edge(From, B, S)`, but new clauses are attached to it by chained-HMAC verification of an optional `caveat` field on the envelope. Operators opt in per edge.

### 5.1 Delegation envelopes (Goblins / Macaroons hybrid)

A node holding an allow-edge mints a sub-cap by appending a delegation caveat. The minted cap is bound to a recipient pubkey (or, in v0, a recipient HMAC key fingerprint) and signed via HMAC-chain so verification is a pure function of the manifest root secret + the caveat sequence.

**Datalog (added clause):**

```
can_invoke(Env) :-
    Env.cap = chain([Root | Caveats]),
    root_edge(Root, A0, B, S),                    % a manifest allow_edge
    verifies(chain, Env.from_secret),             % HMAC chain unbroken
    forall(C in Caveats, satisfied(C, Env)),      % every caveat holds for Env
    Env.payload conforms surface(B, S).schema.
```

**Manifest fragment** (see `example_extended_manifest.yaml` for a complete file):

```yaml
relationships:
  - from: nexus_agent
    to: kanban_node.create_card
    delegable: true                # operator allows this edge to be passed on
    delegation:
      max_depth: 2                 # nexus_agent can mint, the recipient cannot re-mint
      bind_to: pubkey              # caveat must name a recipient pubkey
```

A delegation envelope adds a `cap` field carrying the chained signatures and recipient identity; Core's verifier runs the chain and treats the recipient as the effective `from` for routing.

### 5.2 Caveats (Macaroons + JSON-Schema)

A caveat is a JSON-Schema fragment. The verifier merges the caveat schema with the surface schema (intersection) and validates the payload against the merge. Because mesh already runs `jsonschema.validate` on every payload, the implementation is a one-line schema-merge, not new crypto.

This is the fly.io footgun lesson made concrete: caveats are not free strings, they are typed restrictions in the same vocabulary the surface already speaks.

```yaml
relationships:
  - from: nexus_agent
    to: kanban_node.create_card
    caveats:
      payload:
        properties:
          board_id: { const: "work" }     # may only create on board "work"
          priority: { enum: ["low", "med"] }
```

A delegated cap can append further caveats, never widen them — exactly the macaroon attenuation rule.

### 5.3 Time bounds (Meadowcap)

```yaml
relationships:
  - from: cron_node
    to: webui_node.show_message
    expires_at: "2026-12-31T23:59:59Z"      # absolute deadline
  - from: approval_node
    to: kanban_node.delete_card
    valid_for_seconds: 300                  # rolling lease, refreshed by re-issue
```

The verifier rejects any envelope routed after `expires_at` (or `now() - issued_at > valid_for_seconds`). Audit codes gain `denied_expired`.

### 5.4 Introspection surface

Every node automatically exposes a system surface `_capabilities` answering three questions (the ones the demo answers offline today):

```yaml
# Synthesized by Core at manifest load — operator does not write this.
- id: nexus_agent
  surfaces:
    - name: _capabilities
      type: tool
      invocation_mode: request_response
      schema: ../schemas/system_capabilities.json   # bundled
```

Calls are authorised by an implicit relationship `(*, *._capabilities)` — every node may inspect every other node's authority. The response shape:

```json
{
  "outgoing": [
    {"to": "kanban_node.create_card", "caveats": [...], "expires_at": null},
    ...
  ],
  "incoming": [
    {"from": "human_node"}, ...
  ]
}
```

This is the equivalent of A2A's agent card, scoped to capability not metadata, and matches Plan 9's "every namespace traversal is a permission check": a node walking another node's `_capabilities` learns only the edges Core would route for it.

## 6. Why this combination, and why now

Macaroons alone solve delegation and attenuation, but their freeform-string caveats are a footgun. Meadowcap solves time and path bounds but not arbitrary-payload predicates. Goblins solves three-party handoff but not typed payloads. seL4 solves badges and revocation but lives in kernel-land. **RAVEN_MESH already validates every payload against a JSON-Schema**, which means the missing piece — a typed caveat language — is sitting on disk. The synthesis is: keep the manifest as the *root* of the cap graph, treat each runtime envelope as a chained-HMAC attenuation of a root edge, and let JSON-Schema *be* the caveat language. This is exactly what the inspiration-scout synthesis identified as "the thing nobody has built." The four extensions above are its constituent pieces, additive and independently shippable.

The smallest worthwhile first step is 5.2 (caveats) without 5.1 (delegation): adding payload-shape restrictions to existing edges is a backwards-compatible win that closes weakness 5 immediately, requires no new cryptography, and pays for itself the first time someone narrows `human_node`'s ambient authority to "may not delete cards in the `prod` column." Delegation can come second, once the caveat vocabulary is exercised. Time bounds and introspection surfaces are independent and can ride in any order.

Throughout, the rule that has held mesh together since the v0 prototype — *edge ⇒ allowed, no edge ⇒ denied* — survives. The extensions only add new ways for an edge to *exist*, never new ways to bypass the check. That is the property that makes them a model extension rather than a rewrite.
