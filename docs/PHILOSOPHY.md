# RAVEN Mesh — Design Philosophy

**Status:** companion to `SPEC.md`. `SPEC.md` defines what the protocol
**is**. This document explains **why** it is that shape, and which
decisions are load-bearing.

When the spec and this document disagree on a fact, the spec wins.
When this document and the code disagree on a principle, this document
wins and the principle is the bug-finder.

---

## 1. The core invariant

> **Edge present ⇒ permitted. Edge absent ⇒ denied.**

Authorization is a graph membership test, not a policy engine. The
manifest is the graph. There are no roles, no groups, no priorities,
no scoped tokens. An interaction is allowed if and only if the
manifest declares the relationship.

Every other simplification in the protocol falls out of this. No
delegation primitive, because the manifest is the only source of
edges. No revocation tree, because there is no tree — revoking an
edge revokes exactly that edge. No badges, no minting, no chained
attenuation. The smallest unit of authority is the surface; the
mechanism of grant is an operator (or `core.set_manifest` caller)
adding a line to YAML.

This is a deliberate choice against the canon of mature capability
systems (Goblins / OCapN, seL4, Macaroons, Meadowcap). Each of those
adds runtime cap-passing primitives. RAVEN Mesh refuses, because
runtime cap-passing is the place where mature systems accumulate the
complexity that makes them unportable. Keep the graph static, keep
the mechanism singular, and you keep the protocol implementable in a
weekend.

If a use case demands runtime authority changes, the answer is
`core.set_manifest` plus an approval node, not a new protocol
primitive.

## 2. The layering rule

The protocol is unopinionated. The product is opinionated. They must
remain separable.

**Protocol layer** — what every conformant Core implementation must
provide: `core/`, `node_sdk/`, `schemas/`, `docs/`.

**Opinionated layer** — this build's particular product on top:
`nodes/`, `dashboard/`, `manifests/`, `scripts/`.

The discriminator is the **substitution test**: fork the repo, delete
everything in the opinionated layer, and try to build a totally
different product (a feed reader, a sensor mesh, a webhook pipeline,
a chat-bot stack) on the protocol layer that remains. If you can do
it without editing `core/` or `node_sdk/`, the protocol stayed
unopinionated. If you can't — if a kanban-shaped assumption forced
you to subclass an SDK helper or add a knob to Core — the protocol
leaked opinion and the leak is a bug.

This test is operationalized as a Claude skill at
`~/raven/.claude/skills/protocol-fork-test/SKILL.md`. The agent picks
a fresh alt-product each run, which is deliberate — hard-coding one
alt-product into CI would re-freeze a fresh opinion into the
protocol, which is exactly the failure mode the test exists to catch.

The substitution test is also the deciding test for any proposed
addition to the protocol surface. If the addition only makes sense
because of how this product happens to work, it belongs in the
opinionated layer.

## 3. The `core` node

Core is itself a node. Its control surfaces — manifest reload,
process supervision, audit query, metrics — are mesh surfaces
(`core.*`) reachable only via allow-edges, not a parallel
`/v0/admin/*` admin plane.

**Why this is not "admin endpoints in disguise":** the substitution
test passes more cleanly when control surfaces use the same
authorization mechanism as every other interaction. A mesh whose
admin surface is a graph node can express "this orchestrator agent
may reload the manifest but only this approval node may set it" as
two edges. A mesh whose admin surface is `/v0/admin/*` behind a
bearer token has to invent a parallel role system to express the
same thing.

**Why safety becomes a manifest concern:** an operator who wants
strict separation between mesh participants and mesh controllers
simply writes a manifest with no edges into `core.*`. The protocol
does not have to take a side. An operator who wants the opposite —
an airgapped robot that reconfigures its own mesh in response to
what it learns — writes a manifest that does have those edges. Same
protocol; safety is a graph decision, not a protocol axiom.

**Why `core.invoke_as` is permanently excluded:** synthesizing
envelopes claiming to originate from a different node is identity
spoofing as a protocol primitive. It is a class break of the HMAC
security model — every other guarantee in the spec (audit
attribution, edge enforcement, replay protection) is anchored to
"the signature proves the sender." A surface that lets a holder
forge that anchor invalidates the model. If a node needs to act on
behalf of another principal, that belongs in an opinionated identity
layer above the protocol, not as a mesh edge.

## 4. Stream, not queue

RAVEN Mesh delivers envelopes over live SSE streams. It is not a
message queue, and we resist every pressure to make it one.

**Concretely:** invocations to a disconnected node fail synchronously
with `503 denied_node_unreachable`. They are not queued. The target
will not see them on reconnect. There is no Last-Event-ID resume. A
node that disconnects re-registers on reconnect; the register
response carries fresh state.

**Why:** durability is an application concern, not a protocol one.
Callers that need retries ship them at the call site, with
idempotency keys and a policy they own. Callers that need
guaranteed-delivery semantics deploy an explicit queue node and
declare an edge to it. Both patterns leave the protocol layer alone
and let the durability model match the use case.

Trying to be both a stream and a queue is how you ship a protocol
that is good at neither. Streams have low latency and natural
back-pressure; queues have durability and replay. The boundaries
between them — what gets queued, for how long, in what order, with
what at-most-once / at-least-once semantics — are exactly the
decisions that should live in an opinionated layer.

## 5. What's deliberately out of v0

These were each considered, designed, in some cases sketched. Each
was rejected because an existing primitive already covers the use
case and adding the feature would have expanded protocol surface
without buying capability.

- **Caveats** — typed restrictions narrowing an edge. An approval
  node already filters by content, can ask a human, and lives at the
  manifest layer where policy belongs.

- **Delegation** — runtime cap-passing between nodes. Either the
  sub-agent is internal to a node (the mesh sees one node, no protocol
  move needed) or it is a real node in the graph (`core.set_manifest`
  plus reload puts it there).

- **Ephemeral tokens** — short-lived HMAC tokens for one-off scripts
  or cron jobs. A scripts node (or cron node) registered in the
  manifest covers this with the same primitive every other node uses.
  A second token type would be a parallel auth model.

- **Last-Event-ID resume** — replaying missed SSE events on
  reconnect. Once subscriptions are out (out-of-band metrics and
  audit query cover the observability use case), no consumer needs
  resume — re-register on reconnect carries current state via the
  register response.

- **Capability introspection surfaces (`_capabilities`)** — a
  protocol-built-in way for nodes to ask each other "what may you
  invoke?" Core already returns relationships at registration and
  serves the full graph via out-of-band `/v0/introspect`. Adding a
  per-node introspection surface implies an implicit `(*, *._capabilities)`
  allow-edge, which is policy as a protocol primitive — the exact
  pattern this design refuses.

- **`core.invoke_as`** — see §3. Permanent.

If a future need surfaces that one of these would solve, the burden
of proof is on the addition: show the use case, show that no
combination of existing primitives covers it, show that the substitution
test still passes with the addition. Until then, the protocol stays
as it is.

---

## A note on this document

This file is short because the protocol is small. If it grows
significantly, that is a signal — either the protocol is taking on
opinion (which §2 forbids) or the rationale is being padded
(which §1 makes unnecessary).

The decisions captured here were made between approximately
2026-04-01 and 2026-05-10, in a series of pair-thinking sessions
between the project owner and a coding agent. The git log contains
the contemporaneous design notes that were collapsed into this
document. Read them if archaeology is needed; otherwise trust the
spec and this file.
