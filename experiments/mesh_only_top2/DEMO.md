# mesh_only_top2 — Provenance Replay

A mesh-native capability that **rewinds and re-fires past invocations**.
`replay_node` taps Core's envelope stream, captures every routed envelope
into a correlation-id-indexed store, and exposes four typed mesh surfaces
that let any peer list captured chains, inspect them, re-fire them, mutate
their payloads on re-fire, and diff the results.

The companion `counter_node` is a tiny stateful target whose only job is to
make the time-travel observable: replay a captured chain after resetting the
counter, and the counter's final value matches the original run — the
audit-log + admin-invoke are sufficient to reconstruct execution.

## Layer

**Opinionated.** This experiment lives entirely on top of protocol-layer
primitives Core already exposes:

- HMAC-signed envelopes (every captured envelope arrived signature-valid)
- `correlation_id` (the chain identifier — assigned by Core, not by an SDK)
- `/v0/admin/stream` (live envelope tap with full payloads)
- `/v0/admin/invoke` (synthesize a signed envelope from a chosen registered
  node — the primitive that lets us *re-fire* without resigning)
- JSON-Schema-typed surfaces (the four `replay_*` surfaces and three
  `counter_*` surfaces are validated by Core at the boundary like any other)
- The manifest as authority graph (every replay edge is declared)

**Nothing in this experiment touches `core/`, `node_sdk/`, `schemas/`
(top-level), or any protocol-layer file.** A different product on top of
the same protocol could ignore replay entirely, or wire a totally different
replayer (e.g. one that skips responses and re-routes through a fork).
That's the fork-test outcome we want.

The one wart: `replay_node` reads `/v0/admin/stream` with an `ADMIN_TOKEN`.
This is the same wart `dashboard_node` carries today, and the v1 PRD's
HR-15 closes it by promoting the admin tap to a normal mesh surface
(`core.audit_stream`). When that lands, `replay_node` loses the
`ADMIN_TOKEN` env var and adds a normal manifest edge. **No protocol change
is needed for this experiment to ship — only opinionated-layer cleanup.**

## Why this is mesh-only

Outside of RAVEN_MESH, "replay yesterday's request against today's
services" requires a stack: per-service idempotency keys, an
event-sourcing pipeline, OpenTelemetry-style correlation glue, and bespoke
replay tooling that knows how to re-issue requests with a service's auth.
Each of those is per-team work, and none of them give you cryptographic
verification that the replayed request is the same one that ran before.

Inside the mesh:

- The audit log + admin tap are the event-sourcing pipeline. Every
  envelope already has its full payload, signature, schema-validated
  shape, and its `correlation_id`. No new instrumentation in any node.
- `/v0/admin/invoke` is the replay primitive. It synthesizes a freshly
  signed envelope *from* the original sender's node identity — replay
  doesn't have to forge a signature or hold the original sender's key.
- Schema validation runs again on the replayed envelope. If the surface
  schema has tightened since the capture, the replay fails loudly with
  `denied_schema_invalid` — a free compatibility check.
- The replayed envelopes show up in the audit log too, with a fresh
  `correlation_id` (Core mints one in `handle_admin_invoke`). So the
  replay itself is observable: replay_node is recursively visible to
  itself.

The only line of code per node we'd need to make this work in another
system is zero. That's the demo punchline.

## Surfaces

| surface | payload | response |
| ------- | ------- | -------- |
| `replay_node.list` | `{since?, to_surface?, from_node?, limit?}` | `{chains: [{correlation_id, started_at, first_from, first_to, invocation_count, envelope_count}], total_envelopes}` |
| `replay_node.chain` | `{correlation_id}` | `{correlation_id, envelopes: [...], length}` |
| `replay_node.run` | `{correlation_id, dry_run?, mutate?: {to_surface?, set: {...}}}` | `{source_correlation_id, replay_correlation_id, invocations_replayed, results: [{from_node, to_surface, original_msg_id, original_payload, sent_payload, status, response}]}` |
| `replay_node.diff` | `{left_correlation_id, right_correlation_id}` | `{left_correlation_id, right_correlation_id, rows: [{step, to_surface, left_response, right_response, equal}], all_equal}` |

Schemas live in `experiments/mesh_only_top2/schemas/` and are bound to the
surfaces by `manifests/replay_demo.yaml`. Core enforces them at the wire.

## Run the demo

```bash
bash experiments/mesh_only_top2/demo.sh
```

In about ten seconds the script will:

1. Boot Core on port 8048 with `manifests/replay_demo.yaml`. The script
   generates a fresh `ADMIN_TOKEN` per run (no legacy default — Core
   refuses to start with the placeholder, per HR-2 in the v1 PRD).
2. Boot `counter_node` and `replay_node`. `replay_node` opens an SSE
   connection to `/v0/admin/stream` and starts capturing every envelope.
3. Drive 3 increments through `counter_node.increment` (`by: 1`, `by: 2`,
   `by: 4`) so the counter ends at **7**.
4. Ask `replay_node.list` for the chains it captured — three of them, each
   with one invocation + one response envelope.
5. Pick one and ask `replay_node.chain` for the full envelope sequence,
   including the original payload `{by: 4}` and the response `{value: 7,
   by: 4}`.
6. Reset `counter_node` to zero, then replay each captured chain via
   `replay_node.run`. The counter reaches **7** again. **This is the
   time-travel moment** — no per-actor instrumentation, no idempotency
   keys, no event store, no replay glue. The audit tap and admin/invoke
   are sufficient.
7. Reset again and replay every chain with a `mutate: {to_surface:
   "counter_node.increment", set: {by: 10}}` payload override. The
   counter ends at **30** (3 × 10) instead of 7. This is the A/B
   "what-if" story — same chain shape, different payloads, recorded
   separately, fully audited.
8. Use `replay_node.diff` to align the response payloads of two recent
   replays (one with `by=1`, one with `by=99`). The diff returns
   `equal: false` and prints both responses side by side.
9. Tear everything down.

## What the demo proves

- A node whose only inputs are **the running mesh's envelope tap** and
  **the running mesh's admin/invoke endpoint** can rewind, re-execute,
  and A/B mutate the system's full request history without any node
  ever knowing replay exists.
- `correlation_id` is the natural primary key for "what ran together."
  `replay_node.list` and `replay_node.chain` group envelopes by it
  without any peer cooperation.
- Schema validation on replay is a free compatibility check: the
  re-fired envelope is validated against the *current* surface schema,
  not the schema-at-capture-time. A schema tightening fails the replay,
  not silently corrupts state.
- Replay produces fresh signed envelopes (Core mints them in
  `handle_admin_invoke`) — they carry their own new `correlation_id` and
  show up in the audit log as legitimate, distinguishable invocations.
  Provenance stays intact.

## Files

| path | purpose |
| ---- | ------- |
| `replay_node/replay_node.py` | the capability — admin-stream subscriber, capture store, replayer, four surface handlers |
| `counter_node/counter_node.py` | tiny stateful target (`increment`/`get`/`reset`) |
| `manifests/replay_demo.yaml` | demo manifest with three nodes and seven edges |
| `schemas/replay_*.json` | input schemas for the four replay surfaces |
| `schemas/counter_*.json` | input schemas for the counter surfaces |
| `schemas/demo_actor_inbox.json` | permissive inbox schema for `demo_actor` |
| `demo.sh` | end-to-end demo (~10 seconds) |

## Not built (slated, scoped out of this experiment)

- **Subscribe surface.** A `replay_node.subscribe` that streams new
  captures as they happen, instead of forcing peers to poll
  `replay_node.list`.
- **Persistent state diff.** The current `diff` aligns by step index;
  semantic alignment ("did the counter end at the same place?") would
  require a separate per-target reducer.
- **Replay against a forked manifest.** The full version of brainstorm
  idea #13: boot a second Core with a replacement node behind the same
  surface, replay through it, diff. The replay primitive in this node is
  already exactly what that needs — only the fork-Core boot is missing.
- **`/v0/admin/stream` → `core.audit_stream`.** Drops the `ADMIN_TOKEN`
  dependency. Lands with v1 HR-15.
