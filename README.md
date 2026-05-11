# RAVEN Mesh

A small protocol where every participant — human, agent, tool, device, runtime — is a uniformly-modeled **node** exposing typed **surfaces**, and every interaction crosses a declared **relationship** mediated by a thin **Core**. Core owns identity, routing, and audit. Nothing else. Memory, scheduling, approvals, dashboards — all of those are themselves nodes.

This repo is the **protocol layer**: a single-process Python Core (~430 lines), the Python `node_sdk`, the manifest format, the wire schemas, and the conformance test suite. No node implementations live here, no UI, no example product — those have been moved out so the protocol can be forked and reused for a totally different mesh-shaped product without carrying kanban-shaped opinions.

> **Status:** v0.4 — Python prototype, local-first. The protocol is the contract; this implementation is the conformance test.

## Repo tour

```
core/          single-process Python Core. one file: core/core.py.
node_sdk/      MeshNode helper — every Python node uses this.
schemas/       JSON Schemas:
  manifest.json    manifest validator
  core/            schemas for the core.* surfaces (state, audit_query, etc.)
manifests/     (empty — supply your own and pass --manifest at boot)
scripts/       (empty — run Core directly with python3 -m core.core)
tests/         pytest suite — protocol conformance, ~140 tests
docs/
  SPEC.md         authoritative wire + manifest spec
  PHILOSOPHY.md   why the protocol has the shape it has
mesh.toml.example  TOML config template with comments
```

## Companion library

Example meshes, reference node implementations (cron, webui, human, approval, kanban, voice, nexus agent, dummies for protocol testing), and a browser dashboard live at:

**https://github.com/R-A-V-E-N-delegate/raven-mesh-nodes**

That's where to look for "how do I plug a real node in" or "show me a working demo". Pin it to a specific revision of this protocol repo and it builds on top of the surfaces defined here.

## Experiments archive

Earlier exploration work — alternate-language ports (Elixir, Go, Rust), a NATS-pivot evaluation, a multi-host federation prototype, a causal-chain time-travel debugger, a runtime tool-discovery composer, and the daemon-vs-cold-spawn process-model benchmark — has been moved out to a companion archive repo:

**https://github.com/R-A-V-E-N-delegate/raven-mesh-experiments**

Each subdirectory has its own analysis document recording what was tried and what it concluded. This is a graveyard, not a roadmap — the prototypes are frozen in time and not guaranteed to run today.

## Quick start

```bash
git clone https://github.com/coltonkirsten/RAVEN_MESH.git
cd RAVEN_MESH

pip install aiohttp pydantic pyyaml jsonschema croniter structlog pytest pytest-asyncio

python3 -m pytest                                       # protocol conformance suite
python3 -m core.core --manifest path/to/your.yaml       # boot Core with a manifest
```

Core health: http://127.0.0.1:8000/v0/healthz
Core registry: http://127.0.0.1:8000/v0/introspect

To actually drive a mesh, write a manifest (see SPEC §8) or grab one from the companion library above. Core no longer ships with a default manifest — boot fails fast with a clear error if `--manifest` isn't supplied.

## Configuration

Core's non-secret tunables (host, port, manifest path, admin rate limits, replay window, supervisor toggles, audit log path) load from a TOML file with env-var and CLI overrides. Secrets stay in env vars only.

**Precedence (highest wins):** CLI flag → env var (`MESH_HOST`, `MESH_PORT`, `MESH_MANIFEST`, …) → TOML file → built-in default.

- **TOML file:** `mesh.toml` in the working directory (or `configs/mesh.toml`, or pass `--config path/to/file.toml`, or set `MESH_CONFIG`). See [`mesh.toml.example`](mesh.toml.example) for the full schema with comments.
- **Secrets stay env-only:** `ADMIN_TOKEN` and per-node `identity_secret` (resolved via `env:VAR_NAME` in the manifest) are never read from the TOML.
- **Inspect resolved config:** `python3 -m core.core --dump-config` prints the merged values with a `# from <source>` comment on every line.

## Spec

- **Wire protocol, envelope, manifest format, conformance:** [docs/SPEC.md](docs/SPEC.md)
- **Why the protocol has this shape:** [docs/PHILOSOPHY.md](docs/PHILOSOPHY.md)

## Tests

`tests/test_protocol.py` boots Core in-process and exercises the ten PRD §7 flows, including a non-SDK external node that speaks the protocol with hand-rolled stdlib HTTP. If those pass, the protocol is preserved.

```bash
python3 -m pytest -v
```

## Adding a node in any language

1. Read [docs/SPEC.md](docs/SPEC.md) — that's the whole contract.
2. Add it to a manifest with its declared surfaces and edges.
3. Implement: `POST /v0/register`, an `EventSource` consumer of `GET /v0/stream`, and `POST /v0/respond`. That's it.

The `tests/test_protocol.py::test_step_10_external_language_node` test is a working example — the entire "external" node fits in ~30 lines and uses no SDK.
