> **SUPERSEDED by [docs/SPEC.md](../docs/SPEC.md).** This proposal has landed.
> Treat `docs/SPEC.md` as the authoritative source; this note is preserved
> as historical design context only.

# Proposal: Merge `admin` into the `core` node

**Author:** RAVEN
**Date:** 2026-05-10
**Status:** superseded by docs/SPEC.md (accepted; spec is now authoritative)
**Supersedes:** R5 from `MORNING_BRIEFING.md`; collapses Q4
**Anchors:** `docs/PROTOCOL.md` v0; `notes/2026-05-10_morning_review.md` §14, §15

---

## 1. Problem

The protocol today has two control planes:

- **Mesh plane** — nodes talk via `/v0/register`, `/v0/invoke`, `/v0/respond`,
  `/v0/stream`. Every message is HMAC-signed, schema-validated, and
  edge-checked against the manifest.
- **Admin plane** — `/v0/admin/*` (14 endpoints) bypasses edge checks. Gated
  only by `ADMIN_TOKEN`. Used to spawn nodes, reload manifest, tap audit
  stream, synthesize envelopes.

The admin plane is an opinionated bypass baked into the protocol. It forces
two outcomes the protocol should not be forcing:

1. There is exactly one privileged controller (whoever holds the token).
2. Control is binary — full admin or none.

This breaks the substitution test: fork the protocol, delete every node and
dashboard, and a re-build still inherits a privileged channel that has
nothing to do with the protocol's job (broker identity, edges, schema,
audit).

## 2. Proposal

Remove `/v0/admin/*` from the protocol. Expose Core's control verbs as
surfaces on a single first-class node named `core`. All control reaches it
through the same `/v0/invoke` path every other node uses, gated by the same
relationship + signature + schema rules.

Safety becomes a manifest concern, not a protocol concern. Operators decide
who can spawn/stop/reload by drawing edges into `core.*` surfaces.

## 3. The `core` node

`core` is a reserved node id. Manifest validator rejects user-declared nodes
with `id: core`.

`core` is special in three (and only three) ways:

1. **Built into Core.** Not started as a subprocess; its handlers run inside
   the broker.
2. **Always present.** Listed in every manifest snapshot whether the manifest
   YAML names it or not.
3. **Routes inward.** Envelopes to `core.*` are dispatched to in-process
   handlers instead of pushed to an SSE stream.

Everything else is normal:

- It has a `node_id` (`core`) and an HMAC identity secret
  (`MESH_CORE_SECRET`, env-only — secrets never live in TOML).
- It declares surfaces with JSON schemas under `core/schemas/core/*.json`.
- It is reachable only via allow-edges. No edge to `core.spawn` ⇒ no caller
  can spawn. No edge anywhere into `core` ⇒ Core is uncontrollable from the
  mesh, which is a valid configuration (e.g. a pure broker for a fixed
  manifest).

## 4. Surface inventory

Mapping from today's `/v0/admin/*` endpoints to `core.*` surfaces. Three
outcomes per row: **keep** (becomes a surface), **drop** (delete entirely),
**out-of-band** (stays as a non-mesh HTTP endpoint because the mesh isn't
the right channel).

| Today's endpoint           | Verb     | Outcome      | New surface                 | Notes |
| -------------------------- | -------- | ------------ | --------------------------- | ----- |
| `GET  /v0/admin/state`     | read     | keep         | `core.state` (tool, req/resp) | snapshot read; no side effect |
| `GET  /v0/admin/stream`    | read     | out-of-band  | —                           | SSE tap; can't be modeled as a request/response surface; keep at `/v0/admin/stream` gated by token (see §6) |
| `POST /v0/admin/manifest`  | write    | keep         | `core.set_manifest` (tool)  | accept a manifest YAML, validate, persist |
| `POST /v0/admin/reload`    | write    | keep         | `core.reload_manifest`      | re-read manifest from disk |
| `POST /v0/admin/invoke`    | write    | **drop**     | —                           | identity-spoof primitive; remains a dev CLI tool, not a mesh surface |
| `GET  /v0/admin/processes` | read     | keep         | `core.processes`            | supervisor process listing |
| `POST /v0/admin/spawn`     | write    | keep         | `core.spawn`                | start a supervised process by node id |
| `POST /v0/admin/stop`      | write    | keep         | `core.stop`                 | stop a supervised process |
| `POST /v0/admin/restart`   | write    | keep         | `core.restart`              | restart a supervised process |
| `POST /v0/admin/reconcile` | write    | keep         | `core.reconcile`            | declare-state reconciliation |
| `POST /v0/admin/drain`     | write    | keep         | `core.drain`                | drain in-flight for shutdown |
| `GET  /v0/admin/metrics`   | read     | out-of-band  | —                           | Prometheus-style scrape; not a node-to-node concern; keep at `/v0/admin/metrics` |
| `GET  /v0/healthz`         | read     | unchanged    | —                           | already non-admin, no change |
| `GET  /v0/introspect`      | read     | unchanged    | —                           | already non-admin, no change |

Net: **9 surfaces on `core`**, **2 endpoints stay out-of-band** (`stream`,
`metrics`), **1 dropped** (`invoke`).

## 5. Why `invoke_as` is dropped, not merged

`POST /v0/admin/invoke` lets the caller synthesize a signed envelope claiming
to be from any node. Promoting this to a surface (`core.invoke_as`) means any
node with an allow-edge into it can impersonate any other node. That makes
identity itself negotiable through the relationship graph — a class break.
The substitution test fails: a fresh product on this protocol inherits a
loaded gun.

It stays usable as a developer CLI (`python3 -m core.cli invoke-as ...`) that
operates on a stopped or local Core, not as a runtime surface.

## 6. What stays out-of-band

Two endpoints don't fit the surface model:

- **`/v0/admin/stream`** — SSE tap of every envelope. This is a debugger /
  observability tool, not a node-to-node interaction. Wrapping it in
  envelope-shaped traffic doubles every event. Stays as raw SSE, gated by
  `ADMIN_TOKEN`.
- **`/v0/admin/metrics`** — Prometheus scrape. Standard operator tooling,
  not mesh traffic.

Both keep their token gate because they're for the human operator and
external tooling, not for mesh nodes. The protocol still doesn't mandate
them — a v0-compatible Core implementation can omit them and remain
compliant.

## 7. Schema sketch

Each `core.*` surface gets a JSON schema. Concrete examples (full schemas
in implementation phase):

```json
// core/schemas/core/spawn.json
{"type": "object",
 "required": ["node_id"],
 "properties": {"node_id": {"type": "string"}},
 "additionalProperties": false}

// core/schemas/core/set_manifest.json
{"type": "object",
 "required": ["yaml"],
 "properties": {"yaml": {"type": "string"}},
 "additionalProperties": false}

// core/schemas/core/state.json  (empty input)
{"type": "object", "additionalProperties": false}
```

Response payloads match the JSON shapes today's `/v0/admin/*` endpoints
already return.

## 8. Bootstrap & identity

`core` needs an HMAC secret to verify inbound signatures and to sign its own
responses. Three rules:

1. Secret lives in `MESH_CORE_SECRET` (env var). Never in TOML, never in
   the manifest YAML.
2. Core does **not** self-register. It has no outbound `/v0/register` call —
   it's always present.
3. **Outbound from core.** If `core` ever needs to invoke a surface on
   another node (e.g. lifecycle hooks), it signs with its own secret and
   uses the normal `/v0/invoke` path, subject to the same allow-edge
   checks. Today there's no such use case; the surfaces above are all
   inbound. Keep the door open for future work.

## 9. Manifest changes

Manifests gain an optional convention but no required syntax change.
Operators wire control by declaring relationships into `core.*`:

```yaml
relationships:
  # let the dashboard read state
  - { from: dashboard, to: core.state }
  - { from: dashboard, to: core.processes }
  # let the orchestrator spawn/stop nodes
  - { from: orchestrator, to: core.spawn }
  - { from: orchestrator, to: core.stop }
  # NO edge into core.set_manifest — no node can rewrite the manifest at runtime
```

Manifest validator additions:

- Reject `nodes[].id == "core"`.
- Allow `relationships[].to` to reference `core.<surface>` and validate
  surface exists.

## 10. Migration

Two-step. Both steps land on `simplify-raven`:

**Step A — introduce `core` node alongside `/v0/admin/*`.**
- All `core.*` surfaces work via `/v0/invoke`.
- All `/v0/admin/*` endpoints continue to work as thin shims that call the
  same in-process handlers as the new surfaces.
- Dashboard, audit_tap, and other callers can migrate at their own pace.
- Single Core release; no caller breakage.

**Step B — remove `/v0/admin/*` shims (next minor version).**
- All callers must use `core.*` surfaces by then.
- `/v0/admin/stream` and `/v0/admin/metrics` remain (per §6).
- Delete the shim handlers, the admin rate-limit middleware specific to
  the shims, and the `ADMIN_TOKEN` token-check for the dropped surfaces
  (`ADMIN_TOKEN` still gates `stream` and `metrics`).

Audit log: every routed envelope into `core.*` already gets logged by the
normal `routed` path. The `details` map records the surface, so existing
audit consumers see admin operations in the same stream as everything else
— a strict improvement over today's split.

## 11. Backward compatibility & breaking changes

- **Step A is fully back-compat.** No caller change required.
- **Step B is a breaking change** for anyone hitting `/v0/admin/{state,
  manifest, reload, processes, spawn, stop, restart, reconcile, drain}`
  directly. Bump to `v0.x+1` documentation, keep wire prefix `/v0/`.
- **`/v0/admin/invoke` is dropped immediately in Step A.** No production
  caller exists; it's a dev tool today. CLI replacement ships in the same
  PR.

## 12. Threat model deltas

| Concern | Today | After merge |
| ------- | ----- | ----------- |
| Who can spawn a node? | Anyone with `ADMIN_TOKEN` | Anyone with an allow-edge to `core.spawn` |
| Granular control (read-only operator) | Not possible — admin token is all-or-nothing | Allow-edge to `core.state`, `core.processes` only |
| Approval gates on dangerous ops | Custom — must wrap admin endpoints externally | Native — put an `approval` node between caller and `core.spawn`; identical to PRD §4.2 approval flow |
| Airgapped self-modifying mesh | Token must be embedded in the agent | Edge `robot_brain → core.spawn`; no token; no bypass |
| Spoofing risk from this change | `ADMIN_TOKEN` leak ⇒ full control | `MESH_CORE_SECRET` leak ⇒ ability to **forge core's outbound signatures**, not inbound control. Inbound control still requires the caller's own secret + an allow-edge. |
| Identity spoofing (`invoke_as`) | Possible via `/v0/admin/invoke` (token-gated) | Not possible via mesh (dropped); CLI-only |

Net: strict reduction in protocol-level privilege, plus the existing
approval-flow machinery applies natively to control operations.

## 13. Non-goals

- **No new transport.** All `core.*` traffic uses the existing
  `/v0/invoke` path.
- **No new auth mechanism.** HMAC + allow-edges only. `ADMIN_TOKEN`
  survives only for `stream` and `metrics` operator endpoints.
- **No cross-machine control change.** Cross-machine lifecycle remains the
  open question from §7 of the morning review; this proposal is
  orthogonal.

## 14. Open questions

1. **Reload-self semantics.** When `core.set_manifest` or
   `core.reload_manifest` runs, the active manifest changes. If the caller
   relied on an edge that's now gone, do we close their session? Proposal:
   yes, and emit `manifest_reloaded` SSE events to every affected node.
2. **Should `core.audit_stream` exist as a fire-and-forget surface for nodes
   that want a programmatic tap?** Distinct from the human-debugger SSE.
   Out of scope for this proposal; track separately.
3. **CLI ergonomics.** `python3 -m core.cli invoke core.spawn '{"node_id":
   "tasks"}'` is verbose. A `core-ctl spawn tasks` wrapper helps but is
   tooling, not protocol.

## 15. Recommendation

Ship Step A on `simplify-raven` after one more design review pass. Step B
gates on dashboard + audit_tap migration. Estimated implementation: one
worker, one session, ~6 commits.
