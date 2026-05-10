# Layer audit — mesh_only_top1

Per `notes/PROTOCOL_CONSTRAINT.md`, every recommendation/code-change is tagged
with the layer it touches.

## Summary

**This prototype lives entirely in the OPINIONATED layer.** Zero
protocol-layer changes were required. No file under `core/`, `node_sdk/`,
`schemas/manifest.json`, `tests/test_protocol.py`, or `docs/PROTOCOL.md` was
modified, added, or removed.

## File-by-file ownership

| Path | Layer | Reason |
|------|-------|--------|
| `mesh_chronicle/__init__.py` | OPINIONATED | new node module |
| `mesh_chronicle/recorder.py` | OPINIONATED | observer that consumes the existing `/v0/admin/stream` SSE tap |
| `mesh_chronicle/replayer.py` | OPINIONATED | uses the existing `/v0/admin/invoke` endpoint to synthesize envelopes; uses the existing `/v0/admin/state` to read current schemas |
| `mesh_chronicle/differ.py` | OPINIONATED | helper, no protocol contact |
| `mesh_chronicle/chronicle_node.py` | OPINIONATED | a normal mesh node with surfaces; no privileged access path |
| `mesh_chronicle/echo_capability.py` | OPINIONATED | demo capability |
| `mesh_chronicle/demo_client.py` | OPINIONATED | demo driver |
| `web/index.html` | OPINIONATED | inspector UI |
| `manifests/chronicle_demo_v1.yaml` | OPINIONATED | one specific deployment |
| `manifests/chronicle_demo_v2.yaml` | OPINIONATED | one specific deployment |
| `schemas/echo_v1.json`, `echo_v2.json` | OPINIONATED | demo capability schemas |
| `schemas/chronicle_*.json` | OPINIONATED | this node's surface schemas |
| `scripts/demo.sh`, `scripts/_env.sh` | OPINIONATED | local boot scripts |
| `tests/conftest.py`, `test_chronicle.py` | OPINIONATED | tests for this node |

## Protocol-layer dependencies (unchanged, used as-is)

The chronicle relies on these existing protocol surfaces. None are modified.

| Dependency | What we use it for |
|------------|--------------------|
| HMAC-signed envelopes | tamper-evident recordings; `chronicle.reverify` recomputes |
| `correlation_id` field | groups envelopes into causal chains |
| `/v0/admin/stream` (SSE tap) | source of captured envelopes |
| `/v0/admin/state` | current manifest + per-surface schemas (for compat check) |
| `/v0/admin/invoke` | synthesize a signed envelope from the original sender's identity for replay |
| Manifest schema (`schemas/manifest.json`) | declares chronicle's own surfaces — already supports `additionalProperties: true` so no extension was needed |

## Validation against the constraint

> **Validate by substitution.** Could someone fork RAVEN_MESH, throw away
> every node and the dashboard, build a totally different product on the
> same protocol — and have the protocol still feel right?

The chronicle is itself proof that the answer is **yes**. It's a totally
different product (a debugger), it uses no kanban-/voice-/agent-shaped
assumptions, and it works against the protocol as-shipped. The fork test
(PRD §10 A8) does not regress because of anything in this directory.

## Required protocol changes — none

If a future iteration wants any of these, they would be **protocol-layer**
changes and would need to be proposed separately:

- **Bounded ring-buffer for the admin tap with `Last-Event-ID` resume**
  (already proposed as v1 PRD HR-5). Without it, a chronicle that
  reconnects loses envelopes emitted during the gap. Workaround: the
  chronicle persists to disk per envelope.
- **A read-only flavor of `/v0/admin/invoke` that returns a *signed envelope*
  the chronicle can re-emit on the regular `/v0/invoke` path** without
  needing the admin token. This would let the chronicle work without admin
  privileges. Today's `admin_synthesized: true` audit flag is the safety
  net; the chronicle accepts it.
- **A protocol-defined `_capabilities` self-surface** (v1 PRD HR-9).
  Currently the chronicle reads `/v0/admin/state` because that surface
  doesn't exist yet. Once HR-9 lands, the chronicle would prefer the
  protocol-blessed introspection path over the admin one.

None of these are required for the prototype to work today, and none are
implemented in this directory.
