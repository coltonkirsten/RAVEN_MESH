# RAVEN Mesh

A small protocol where every participant — human, agent, tool, device, runtime — is a uniformly-modeled **node** exposing typed **surfaces**, and every interaction crosses a declared **relationship** mediated by a thin **Core**. Core owns identity, routing, and audit. Nothing else. Memory, scheduling, approvals, dashboards — all of those are themselves nodes.

This repo is the **v0 reference implementation**: a single-process Python Core (~430 lines) plus four real reference nodes (cron, webui, human dashboard, approval) and four protocol-test dummies.

> **Status:** v0.4 — Python prototype, local-first. The protocol is the contract; the prototype is the conformance test. The BEAM/Elixir refactor is a future state, not this build.

## Quick start

```bash
git clone https://github.com/coltonkirsten/RAVEN_MESH.git
cd RAVEN_MESH

pip install aiohttp pydantic pyyaml jsonschema croniter structlog pytest pytest-asyncio

python3 -m pytest                    # all 19 tests pass
scripts/run_demo.sh start            # protocol-validation demo (Core + tasks + approval)
scripts/run_full_demo.sh start       # all four real nodes wired together
```

Once `run_full_demo.sh` is up:

| dashboard | url |
| --------- | --- |
| webui_node    | http://127.0.0.1:8801 |
| human_node    | http://127.0.0.1:8802 |
| approval_node | http://127.0.0.1:8803 |
| Core health   | http://127.0.0.1:8000/v0/healthz |
| Core registry | http://127.0.0.1:8000/v0/introspect |

Try: open http://127.0.0.1:8801 (webui), then in the human dashboard at http://127.0.0.1:8802 pick `webui_node.show_message` and send `{"text": "hello from human"}`. The webui browser updates live.

Stop everything: `scripts/run_full_demo.sh stop`.

## Repo tour

```
core/          single-process Python Core. one file: core/core.py.
node_sdk/      MeshNode helper — every Python node uses this.
nodes/
  dummy/         protocol-test dummies (actor, capability, approval, hybrid)
  cron_node/     hybrid; persists schedules to data/crons.json
  webui_node/    capability with browser dashboard on :8801
  human_node/    actor with dashboard on :8802 (inbox + invoke any allowed surface)
  approval_node/ approval with dashboard on :8803 (Approve / Deny pending requests)
schemas/       JSON Schemas referenced by manifests
manifests/
  demo.yaml         protocol-validation demo (drives tests/)
  full_demo.yaml    all four real nodes
scripts/       bash wrappers; source scripts/_env.sh for deterministic dev secrets
tests/         pytest suite — all ten PRD §7 flows pass
docs/
  PROTOCOL.md     language-agnostic protocol spec — write a node in any language
  PROTOTYPE.md    how this Python implementation is structured + how to refactor away
```

## Configuration

Core's non-secret tunables (host, port, manifest path, admin rate limits, replay window, supervisor toggles, audit log path) load from a TOML file with env-var and CLI overrides. Secrets stay in env vars only.

**Precedence (highest wins):** CLI flag → env var (`MESH_HOST`, `MESH_PORT`, …) → TOML file → built-in default.

- **TOML file:** `mesh.toml` in the working directory (or `configs/mesh.toml`, or pass `--config path/to/file.toml`, or set `MESH_CONFIG`). See [`mesh.toml.example`](mesh.toml.example) for the full schema with comments.
- **Secrets stay env-only:** `ADMIN_TOKEN` and per-node `identity_secret` (resolved via `env:VAR_NAME` in the manifest) are never read from the TOML.
- **Inspect resolved config:** `python3 -m core.core --dump-config` prints the merged values with a `# from <source>` comment on every line.

## Spec

- **Wire protocol & envelope:** [docs/PROTOCOL.md](docs/PROTOCOL.md)
- **Prototype internals & runbook:** [docs/PROTOTYPE.md](docs/PROTOTYPE.md)
- **PRD (v0.4):** lives in the parent `raven` workspace at `context/research/raven_mesh_v0_prd.md`

## Tests

`tests/test_protocol.py` boots Core in-process and exercises all ten flows from PRD §7, including a non-SDK external node that speaks the protocol with hand-rolled stdlib HTTP. If those pass, the protocol is preserved.

```bash
python3 -m pytest -v
```

## Adding a node in any language

1. Read [docs/PROTOCOL.md](docs/PROTOCOL.md) — that's the whole contract.
2. Add it to a manifest with its declared surfaces and edges.
3. Implement: `POST /v0/register`, an `EventSource` consumer of `GET /v0/stream`, and `POST /v0/respond`. That's it.

The `tests/test_protocol.py::test_step_10_external_language_node` test is a working example — the entire "external" node fits in ~30 lines and uses no SDK.
