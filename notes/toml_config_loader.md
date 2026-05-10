# TOML Config Loader for Core

**Author:** RAVEN worker (config loader task)
**Date:** 2026-05-10
**Branch:** simplify-raven
**Resolves:** Option A from the morning review §12

## Goal

Migrate Core's twelve scattered `os.environ.get(...)` reads into a single, version-controlled, per-mesh TOML file. Env vars and CLI flags remain as overrides. Secrets stay env-only.

## Layer

This is a **protocol-layer change** (per `notes/PROTOCOL_CONSTRAINT.md`). The schema is operator-facing only — every field is a Core-side knob (host, port, replay window, supervisor toggles, audit log path). Nothing in here describes node behaviour. A fork that throws away every node still uses the same config schema unchanged.

## Schema choices

```toml
[server]      host, port, manifest_path, invoke_timeout_s
[admin]       rate_limit, rate_burst                       # ADMIN_TOKEN stays env
[security]    replay_window_s                              # bounds [5, 300] in code
[supervisor]  enabled, log_dir, auto_reconcile
[logging]     audit_log_path
```

- **Per-mesh**, not per-node — one TOML file alongside the manifest yaml.
- **Defaults baked into dataclasses** (`core/config.py`). An empty TOML behaves identically to no TOML.
- **Replay-window bounds stay in code**, not config. The TOML may name a value; out-of-range values get clamped at load time with a `WARNING mesh.config: ...` line.
- **Forward-compat**: unknown sections / unknown keys are logged and ignored, not fatal. Lets us add fields later without breaking older Cores reading newer files.
- **Type mismatches** (e.g. `port = "string"`) fall back to the previous layer's value with a warning, instead of crashing boot.

## Precedence (highest wins)

1. **CLI flag** — e.g. `--host 0.0.0.0`. Argparse defaults are now `None` so the loader can tell "not passed" from "passed default".
2. **Env var** — e.g. `MESH_HOST`. Twelve names preserved verbatim for back-compat.
3. **TOML file** — `mesh.toml` resolved as: `--config <path>` → `$MESH_CONFIG` → `./mesh.toml` → `./configs/mesh.toml` → none.
4. **Built-in default** — baked into the `Config` dataclass.

Every field also records its `source` (`"defaults" | "<toml-path>" | "env MESH_X" | "CLI --x"`) for `--dump-config` introspection.

## Migrated env vars

| Env var (still works as override) | TOML location |
|---|---|
| `MESH_HOST` | `[server] host` |
| `MESH_PORT` | `[server] port` |
| `MESH_MANIFEST` | `[server] manifest_path` |
| `MESH_INVOKE_TIMEOUT` | `[server] invoke_timeout_s` |
| `MESH_ADMIN_RATE_LIMIT` | `[admin] rate_limit` |
| `MESH_ADMIN_RATE_BURST` | `[admin] rate_burst` |
| `MESH_REPLAY_WINDOW_S` | `[security] replay_window_s` |
| `MESH_SUPERVISOR` | `[supervisor] enabled` |
| `MESH_SUPERVISOR_LOG_DIR` | `[supervisor] log_dir` |
| `MESH_AUTO_RECONCILE` | `[supervisor] auto_reconcile` |
| `AUDIT_LOG` | `[logging] audit_log_path` |
| `ADMIN_TOKEN` | **NOT MIGRATED** — secret, env-only |

Per-node `identity_secret` (`env:VAR_NAME` pattern in the manifest) is also untouched and stays env-only.

New env var: `MESH_CONFIG` — points at the TOML file (resolved before the auto-discovery defaults).

## `--dump-config` output sample

```
$ python3 -m core.core --config mesh.toml.example --port 9000 --dump-config
# Resolved config (TOML loaded from mesh.toml.example)

[server]
host = "0.0.0.0"  # from env MESH_HOST
port = 9000  # from CLI --port
manifest_path = "manifests/demo.yaml"  # from mesh.toml.example
invoke_timeout_s = 30  # from mesh.toml.example

[admin]
rate_limit = 60  # from mesh.toml.example
rate_burst = 20  # from mesh.toml.example

[security]
replay_window_s = 60  # from mesh.toml.example

[supervisor]
enabled = false  # from mesh.toml.example
log_dir = ".logs"  # from mesh.toml.example
auto_reconcile = false  # from mesh.toml.example

[logging]
audit_log_path = "audit.log"  # from mesh.toml.example
```

Every line carries `# from <source>`. Operators can answer "what is Core actually using right now?" without grepping env or reading code.

## Test coverage (`tests/test_config.py`, 15 cases)

- defaults when no TOML, no env
- TOML values applied when present
- env overrides TOML
- CLI overrides env
- replay window clamped high (1000 → 300, with warning)
- replay window clamped low (0 → 5, with warning)
- replay window invalid env value (`"not_a_number"` → default, with warning)
- port wrong type (`"not_a_number"` in TOML → default, with warning)
- missing `[section]` in TOML → defaults used for that section
- unknown key + unknown section → logged warning, not fatal
- invalid env value → warning + fall back
- env-bool only `"1"` is True (legacy semantics preserved)
- missing TOML file path → warning, no crash
- `dump_config_toml()` carries source attribution per line
- CLI `store_true` with `default=None` only applies when actually present (verifies the "not set" sentinel works)

Full Core test suite: **172 passed** (was 152 baseline + 15 mine + 5 from the parallel `/v0/register` worker).

## Files changed

```
A  core/config.py              # new — Config dataclass, load_config(), dump_config_toml()
A  mesh.toml.example           # new — sample at repo root with full schema + comments
A  tests/test_config.py        # new — 15-case unit suite
M  core/core.py                # imports config, CoreState takes Config, make_app takes Config,
                               # _build_admin_rate_limiter takes Config, MESH_INVOKE_TIMEOUT read
                               # via state.config, main() builds Config, --config + --dump-config flags
M  README.md                   # new "Configuration" section
```

`_load_replay_window_s()` and the `REPLAY_WINDOW_*` constants in `core/core.py` are preserved (re-imported from `core.config`) so `tests/test_replay_protection.py` keeps passing without modification.

## Coordination with worker 5c20a383

The other worker added a timestamp-window check on `/v0/register` (commit `4f36a43`). Their changes touch `handle_register` lines ~370-388. My changes touch:

- module imports (top of file)
- `CoreState.__init__` (state init)
- `_route_invocation` timeout line (one line)
- `_build_admin_rate_limiter` (rate limiter constructor)
- `make_app` (signature + body)
- `amain` (signature + body)
- `main` + new `_resolve_config_path` (CLI parsing)

Disjoint regions; no merge conflict. The worker's commit was already in `origin/simplify-raven` when I started layering; I built on top of it.

## Follow-up cleanup (out of scope)

- The legacy `_load_replay_window_s()` in `core/core.py` is dead code from Core's perspective once the config layer is in place. Kept for now because `tests/test_replay_protection.py` imports and exercises it directly. A follow-up should rewrite that test to call into `core.config._validate_replay_window` and delete the legacy function.
- `state.replay_window_s` is now copied from `state.config.security.replay_window_s` at init. The duplicate field could be removed in favour of reading through `state.config` everywhere, but that's a wider rename and `state.replay_window_s` is referenced in 3+ places.
- Consider moving `MESH_INVOKE_TIMEOUT` to a `float`-typed field. Currently typed `int` to match the example schema; legacy code parsed via `float()`. No behaviour change but the type annotation is slightly imprecise.
