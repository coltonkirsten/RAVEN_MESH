# dashboard_node v2 — refining the bold proposal

**Author:** worker (refining synthesis §6)
**Date:** 2026-05-10
**Status:** draft proposal, supersedes synthesis_20260510.md §6

> **Note on inputs.** This refines `synthesis_20260510.md` §6 ("make the dashboard a real mesh node") and `sse_consolidation.md`. The referenced `experiments/dashboard_node/REVIEW.md` does not exist on disk; if a prototype review is later landed there, this doc should be re-reconciled against it.

---

## The reframe (read this first)

The v1 proposal said "make the dashboard a real mesh node." Re-read against `PROTOCOL_CONSTRAINT.md`, that framing leaks opinion: it makes "the dashboard" sound load-bearing for the protocol. **It is not.** The dashboard is one specific operator UI we happen to ship. Tomorrow it could be a CLI, a Slack bot, a voice operator, or a TUI. The protocol must not care.

So the work splits cleanly into two layers, and v2 keeps them strictly apart:

- **Protocol layer (unopinionated):** Core gets a small set of *self-surfaces* — surfaces declared by Core itself, callable as ordinary mesh invocations, edge-gated like everything else. This deletes the parallel `/v0/admin/*` protocol entirely. There is no mention of "dashboard" in this layer.
- **Opinionated layer (replaceable):** `nodes/dashboard_node/` is a normal capability node that happens to render a React app and happens to hold edges to the Core self-surfaces. Anyone could fork the repo, throw the dashboard away, and build a totally different operator UI on top of the same self-surfaces. That is the substitution test from PROTOCOL_CONSTRAINT.md §5.

---

## Layer 1: Protocol — Core self-surfaces (PROTOCOL-LAYER)

**Tag: PROTOCOL-LAYER.** Generic, must work for any future operator UI.

Core declares itself as a node-id `core`, kind `broker`, with the following surfaces. They replace today's `/v0/admin/*` endpoints one-for-one. They are edge-gated and HMAC-signed like every other surface.

| Self-surface | Replaces | Mode | Notes |
|---|---|---|---|
| `core.state` | `GET /v0/admin/state` | request_response | Returns nodes, edges, manifest hash, tail. |
| `core.audit_stream` | `GET /v0/admin/stream` | inbox (SSE-fanout) | Subscriber receives every envelope Core sees. |
| `core.set_manifest` | `POST /v0/admin/manifest` | request_response | Body: `{yaml: str}`. Returns structured diff (added/removed/changed nodes + edges). |
| `core.reload_manifest` | `POST /v0/admin/reload` | request_response | Re-read from disk; same diff. |
| `core.invoke_as` | `POST /v0/admin/invoke` | request_response | Synthesize signed envelope on behalf of `from_node`. Privileged — see Risks. |
| `core.lifecycle.spawn` / `.stop` / `.restart` / `.reconcile` | future supervisor admin endpoints | request_response | Mesh-native supervisor API (synthesis §3 question 2). |
| `core.processes` | `/v0/admin/processes` | request_response | Process state from supervisor. |

**Why this is protocol-layer, not opinionated:** every one of these surfaces is something *any* mesh broker would expose to *any* operator client. The dashboard is one such client. A Codex CLI operator agent would hit the same surfaces. The Elixir rewrite would implement the same surfaces. The substitution test passes.

**Wire contract.** Core registers itself in `connections` with a synthetic `secret = ADMIN_TOKEN` (or a random token written to `.core_node_secret` on first boot — see Risks). It is the only node that does not connect via `POST /v0/register` because it cannot register with itself. Treat the Core self-node as implicit; document the asymmetry in PROTOCOL.md as "the broker is a node-shaped peer of itself."

**Edge gating.** `manifest.yaml` declares edges normally:
```yaml
edges:
  - from: dashboard_node
    to: core.state
  - from: dashboard_node
    to: core.audit_stream
  - from: dashboard_node
    to: core.set_manifest
```
A node with no edge to `core.set_manifest` cannot rewrite the manifest, even with a valid HMAC. The admin token foot-gun (synthesis §4) collapses to a single fact: whoever controls the `dashboard_node` HMAC secret controls whatever surfaces the manifest grants `dashboard_node` — same as every other node.

**SSE delivery.** `core.audit_stream` is just an inbox surface that fans out via `node_sdk.SSEHub` (already extracted, see `sse_consolidation.md`). The "live logs" SSE on `/v0/admin/stream` and the `core_audit_stream` inbox surface become the same code path. Sixth bespoke SSE loop avoided.

**Out of scope for the protocol.** The dashboard's page layout, the "Try It" panel, the Mesh Builder UX, the columns on the Live Logs view, the React tooling — none of this appears in PROTOCOL.md. If it did, that would be the leak.

---

## Layer 2: Opinionated — `nodes/dashboard_node/` (OPINIONATED-LAYER)

**Tag: OPINIONATED-LAYER.** Specific UI choice, replaceable.

`nodes/dashboard_node/` becomes a normal capability node, structurally identical to `kanban_node` or `webui_node`:

- A `MeshNode` subclass that registers via HMAC.
- An aiohttp inspector app on its own port (e.g. `8810`) that serves the existing Vite/React build under `/`.
- An `SSEHub` for browser fan-out — the React app subscribes to `dashboard_node`'s own `/events` stream (browser → dashboard_node), and dashboard_node subscribes to `core.audit_stream` (dashboard_node → Core via mesh SSE).
- A small TypeScript mesh client (`dashboard/src/lib/mesh.ts`) that signs envelopes from the browser side. Where the HMAC secret lives is a v2 design choice (see Risks).
- Outgoing edges declared in the demo manifest exactly equal to what the operator UI is supposed to do.

The React code that today calls `fetch('/v0/admin/manifest', {token})` becomes `mesh.invoke('core.set_manifest', {yaml})`. Same response shape. Same auth model as every other node-to-surface call.

**Why this is opinionated-layer:** the choice of React, of Vite, of a Mesh Builder pane vs. a textarea, of which surfaces show up in the side panel — all product decisions. Different teams will want different operator UIs. The Elixir rewrite does not need to port this.

**Substitution check.** Delete `nodes/dashboard_node/` and `dashboard/`. The mesh still runs. Operators can drive it from `curl` against `/v0/invoke` with a `core.set_manifest` envelope. The protocol is unaffected. ✓

---

## Migration steps (concrete)

Six PRs, each independently revertible. Order matters.

**PR 1 — `core.state` self-surface (PROTOCOL-LAYER).** Add Core self-registration on boot, declare `core.state` surface, route invocations to the existing `_handle_admin_state` body. Keep `/v0/admin/state` as a thin shim that constructs an internal envelope. **Acceptance:** existing dashboard works unchanged; new test invokes `core.state` from a real node and gets the same payload.

**PR 2 — `core.audit_stream` self-surface (PROTOCOL-LAYER).** Replace `_admin_streams` with an `SSEHub` on the Core self-node and surface it as `core.audit_stream` (inbox). Keep `/v0/admin/stream` as a shim that calls into the same hub. **Acceptance:** dashboard live logs still work; new test subscribes via mesh SSE and sees envelopes.

**PR 3 — `core.set_manifest` + `core.reload_manifest` (PROTOCOL-LAYER).** Surfaces declared, schemas published. Body returns a structured diff (synthesis §3 question 2). Shim the old endpoints. **Acceptance:** today's dashboard "save manifest" still works through the shim; a node test issues `core.set_manifest` and round-trips.

**PR 4 — Spin up `nodes/dashboard_node/` (OPINIONATED-LAYER).** Wrap the existing Vite build behind a `MeshNode`. Move `dashboard/` Vite output to be served by `dashboard_node`. Browser still hits `/v0/admin/*` for now. **Acceptance:** `run_<dashboard_node>.sh` exists; `run_mesh.sh` brings the dashboard up via the same path as every other UI-bearing node.

**PR 5 — Browser TS client + cut-over (OPINIONATED-LAYER).** Add `dashboard/src/lib/mesh.ts`. Migrate `Try It`, `Mesh Builder`, `Live Logs`, `Surface Inspector` to call mesh surfaces instead of `/v0/admin/*`. Demo manifest grows the explicit `dashboard_node → core.*` edges. **Acceptance:** dashboard works with `ADMIN_TOKEN` set to an unguessable random value (proves the React app no longer needs it, only the dashboard_node HMAC secret).

**PR 6 — Delete the shim layer (PROTOCOL-LAYER).** Remove `/v0/admin/*` endpoints. Remove the admin-token middleware. Remove `_admin_streams`. Delete `_admin_authed`. **Acceptance:** repo grep for `/v0/admin/` returns zero hits in `core/`; only `nodes/dashboard_node/` references the new surfaces. PROTOCOL.md updated with a "Core self-surfaces" section.

**Out of scope for this migration.** `core.invoke_as` and `core.lifecycle.*` are listed in the protocol layer but should ship as separate PRs after the supervisor decision (synthesis §3 question 2) is made. Don't widen the surface area before the supervisor contract is real.

---

## Risks and rollback

**Risk 1 — Browser HMAC secret exposure.** The TS client signs envelopes from the browser. If the secret ships in the bundle, anyone who can `view-source` has it. **Mitigation options, ranked:** (a) `dashboard_node` proxies — browser holds a session cookie scoped to the local Vite host, dashboard_node holds the HMAC secret server-side and re-signs. This is the recommended path; the browser is just dashboard_node's UI tier. (b) browser holds the secret, `MESH_HOST=127.0.0.1` only, accept the local-only trade. **Rollback:** if (a) is too much work, ship (b) with a `bind=127.0.0.1` enforced in dashboard_node and revisit when remote access is on the table.

**Risk 2 — Core self-node bootstrap is asymmetric.** Core can't HMAC-register with itself. Two options: (a) Core treats its own `node_id="core"` as implicitly registered with a secret read from `CORE_SELF_SECRET` env (or generated to `.core_secret` 0600 on first boot); (b) skip the registration step entirely — Core's surfaces are routed directly without going through the connection table. **Recommend (b)** — fewer moving parts, and PROTOCOL.md can be honest that the broker is a peer of itself with a special exemption from registration. **Rollback:** revert PR 1; the `_handle_admin_state` body still exists.

**Risk 3 — Edge-gating regression.** Today's dashboard can talk to *any* surface via `/admin/invoke`. After the cut-over, it can only talk to surfaces explicitly granted in the manifest. The "Try It" panel will silently break for un-edged surfaces. **Mitigation:** the demo manifest pre-grants `dashboard_node → *` for every public surface in the repo, with a comment that production deployments should narrow this. The dashboard's UI shows a clear "no edge granted" error rather than a 401. **Rollback:** widen the demo manifest's edges; this is opinionated-layer config, not protocol.

**Risk 4 — Manifest write race.** `core.set_manifest` and concurrent `core.reload_manifest` from two operators can step on each other. **Mitigation:** Core holds a write lock on `manifest_path`; second writer gets a `409` mesh-level error response with the current manifest hash. **Rollback:** existing behaviour is to last-write-wins; we can revert to that if the lock causes issues.

**Risk 5 — BEAM-rewrite spec growth.** Adding 6+ self-surfaces to the protocol is a real expansion. **Mitigation:** every self-surface has a small, mechanical implementation; the BEAM port is bounded. The alternative (keep `/v0/admin/*` parallel forever) is worse — two protocols to port. **Rollback:** none needed; this is the simplification.

**Risk 6 — Slow consumer on `core.audit_stream`.** Synthesis §4 already flagged: unbounded queues on Core's SSE. The hub already drops on full (`sse_consolidation.md`). Bound at 1024. **Rollback:** trivially bounded; no dependency on the rest of this proposal.

**Global rollback.** PRs 1–3 are pure additions (shims keep old endpoints alive); revert any one in isolation. PR 6 is the only destructive PR — if PR 5 has issues post-merge, hold PR 6 indefinitely and live with both surfaces. Not pretty, but safe.

---

## What this proposal does NOT do

- It does **not** make the dashboard part of the protocol. The protocol gains *generic* self-surfaces; the dashboard is one consumer.
- It does **not** require the supervisor work to land first. PRs 1–6 ship independently of the lifecycle surfaces.
- It does **not** change the envelope schema, signing rules, or registration flow.
- It does **not** prescribe a UI. Operators can write a CLI against `core.state` + `core.set_manifest` and never touch the React app.

If a future contributor reads PROTOCOL.md after this lands, they should be able to fork the repo, delete `dashboard/` and `nodes/dashboard_node/`, and build a totally different operator UI on the same self-surfaces. That's the test.
