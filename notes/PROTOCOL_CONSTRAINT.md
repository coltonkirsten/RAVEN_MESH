# Protocol vs Opinionated Layer — Architectural Constraint

**Author:** Colton Kirsten
**Date:** 2026-05-10 01:42 PT
**Status:** HARD CONSTRAINT — applies to ALL Wave 2 and Wave 3 work

## The constraint

The RAVEN_MESH **protocol** must remain an **unopinionated building block**.

The **dashboard** and **nodes** we have shipped today (kanban_node, voice_actor, webui_node, dashboard_node prototype, etc.) are **opinionated** — they encode specific product decisions about how to use the protocol.

These are two different layers and they must stay separable.

## What this means concretely

### The protocol layer (unopinionated, must stay generic)
- `core/core.py` — envelope routing, signing, manifest enforcement, admin API
- `core/supervisor.py` — process supervision contract
- The envelope schema itself (from, to, surface, body, signature, nonce)
- HMAC signing rules, manifest schema, allow-edge ACL semantics
- The `/v0/admin/*` API contracts (spawn/stop/restart/reconcile/processes)
- `node_sdk/` — generic helpers that any node could use

### The opinionated layer (specific to today's product, replaceable)
- `nodes/kanban_node/` — one specific use case
- `nodes/voice_actor/` — another specific use case
- `nodes/webui_node/` and `nodes/dashboard_node/` — UI surface choices
- `dashboard/` (the React app) — UI presentation choice
- The current manifest.yaml content — one specific deployment

## Implications for your worker task

If you are writing architecture docs, PRDs, migration plans, or making protocol changes:

1. **Do not bake node-specific or dashboard-specific assumptions into the protocol.** If your design only works because we have a kanban_node, it is opinionated and belongs in the opinionated layer.

2. **The protocol is the moat.** It should support kanban + voice + dashboard + 100 use cases we haven't thought of. Do not narrow it.

3. **Identify which layer your work touches.** State explicitly in your output: "this is a protocol-layer change" or "this is an opinionated-layer change." Don't conflate them.

4. **When in doubt, separate.** If a feature could plausibly belong in either layer, prefer pushing it down into the opinionated layer and keeping the protocol surface minimal.

5. **Validate by substitution.** Could someone fork RAVEN_MESH, throw away every node and the dashboard, build a totally different product on the same protocol — and have the protocol still feel right? If the answer is no, you've leaked opinion into the protocol.

## Examples

| Decision | Layer |
|---|---|
| Adding a new restart strategy `on_demand` to supervisor | Protocol (generic) |
| Hardcoding "kanban_node always uses on_demand" in supervisor | Opinionated (wrong layer — belongs in manifest config) |
| The envelope shape | Protocol |
| What columns the dashboard shows | Opinionated |
| The admin API endpoint paths | Protocol |
| The dashboard's specific page layout | Opinionated |
| HMAC signing of envelopes | Protocol |
| The voice_actor's choice to use OpenAI vs Anthropic | Opinionated |

## Apply this lens to every recommendation

Before submitting your output, re-read it once asking: "did I leak opinion into the protocol?" If yes, pull it back into the opinionated layer.
