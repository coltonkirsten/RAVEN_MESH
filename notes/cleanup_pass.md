# Cleanup pass — 2026-05-10

**Author:** worker (Codex)
**Scope:** non-functional cleanup of `core/`, `node_sdk/`, and `nodes/`. No logic changes.
**Tests:** `pytest -x -q` — 145 passed before, 145 passed after. Baseline preserved.

---

## Methodology

Tools used:
- `pyflakes core/ node_sdk/ nodes/` — dead imports.
- `vulture --min-confidence 60` — unused functions / attributes / variables.
- AST scan (custom) — public function/class missing docstring or type hints, in `core/` and `node_sdk/`.
- `grep -n -i 'TODO|FIXME|XXX|HACK'` over all `*.py`.
- AST + grep scan of `core/` for string/import references to specific node IDs (`kanban`, `voice_actor`, `webui`, `dashboard_node`, `nexus_agent`, `approval_node`, `cron_node`, `human_node`, `mesh_db`).

---

## What changed

### Dead imports removed (PROTOCOL + OPINIONATED layers)

| File | Layer | Removed import |
|---|---|---|
| `core/supervisor.py:58` | protocol | `import json` |
| `core/supervisor.py:64` | protocol | `from dataclasses import field` (kept `dataclass`) |
| `node_sdk/sse.py:34` | protocol | `from typing import Awaitable` |
| `nodes/voice_actor/audio_io.py:19` | opinionated | `import math` |
| `nodes/voice_actor/realtime_client.py:12,16` | opinionated | `import asyncio`, `Awaitable`, `Callable` |
| `nodes/kanban_node/kanban_node.py:33` | opinionated | `from typing import Any` |
| `nodes/nexus_agent/agent.py:23` | opinionated | `import hashlib` |
| `nodes/nexus_agent_isolated/agent.py:34` | opinionated | `from .docker_runner import CliResult` |

Verification: `pyflakes` is now clean across all three trees.

### Public-API docstrings added (PROTOCOL layer only)

Focused on the SDK surface and supervisor — these are the contracts node authors and operators interact with. Skipped internal HTTP handlers in `core/core.py` (those are wired by URL, not imported).

| File | Symbol | Layer |
|---|---|---|
| `node_sdk/__init__.py` | `now_iso`, `canonical`, `sign` | protocol |
| `node_sdk/__init__.py` | `MeshError`, `MeshDeny`, `MeshNode` (class doc) | protocol |
| `node_sdk/__init__.py` | `MeshNode.on`, `start`, `stop`, `invoke`, `respond` | protocol |
| `node_sdk/sse.py` | `SSEHub.add_subscriber`, `remove_subscriber`, `broadcast` | protocol |
| `core/supervisor.py` | `ChildState` (class doc), `ChildState.to_dict` | protocol |
| `core/supervisor.py` | `Supervisor` (class doc — restates the layer constraint) | protocol |
| `core/supervisor.py` | `Supervisor.stop`, `restart`, `list_processes`, `shutdown_all` | protocol |

All docstrings are one-liners describing the contract. No logic touched.

### TODO/FIXME comments
None found in any `*.py` file. Nothing to do.

---

## What was deferred (intentionally)

### Vulture findings — not deleted, intentionally retained

Vulture (60% confidence) flagged the following as "unused." Each is actually a public protocol API or a load-bearing internal callback. **Do not delete.**

| File:line | Symbol | Why kept |
|---|---|---|
| `core/manifest_validator.py:53` | `validate_manifest` | Used by `tests/test_manifest_validator.py` and referenced from PRD/postmortem notes. Public API. |
| `core/supervisor.py:303` | `Supervisor.ensure_running` | Public API for on-demand spawn. Documented contract; meant to be called by Core dispatcher (integration not yet wired — see `notes/migration_path.md`). |
| `core/supervisor.py:360` | `Supervisor.can_accept` | Public protocol predicate for graceful drain. Same: caller integration TBD. |
| `core/supervisor.py:371` | `Supervisor.begin_work` | Same — drain in-flight counter. |
| `core/supervisor.py:382` | `Supervisor.end_work` | Same. |
| `nodes/nexus_agent/agent.py:386`, `..._isolated/agent.py:399` | `runtime` attribute | Held for the lifetime of the node; vulture doesn't see attribute reads through `state`. |
| `nodes/nexus_agent*/mcp_bridge.py` | `_list_tools`, `_call_tool` | Registered as MCP handlers via decorator; vulture can't follow that. |
| `nodes/voice_actor/audio_io.py` | `FRAME_BYTES`, `_loop`, `_last_audio_ts`, `is_speaking` | Stored for diagnostics / referenced via runtime hooks. |
| `nodes/voice_actor/realtime_client.py` | `commit_audio`, `clear_audio_buffer`, `create_assistant_text`, `cancel_response` | Public OpenAI Realtime client methods, retained for API completeness even if no current call site. |
| `nodes/voice_actor/audio_io.py:122,234` | `time_info` (100% confidence) | PortAudio callback signature — required parameter name even when unused. |
| `nodes/nexus_agent/web/server.py:31,35` | `logs_dir`, `runtime` | aiohttp `web.AppKey`-style attachments accessed via the request object. |

### Docstrings deferred — internal HTTP handlers in `core/core.py`

The following lack docstrings but are aiohttp request handlers wired by URL routing rather than imported. They are not really "public APIs" in the SDK sense. Adding docstrings here is low-value boilerplate. **Defer until we do a doc pass on the admin API contract** (which `notes/v1_prd_draft.md` already calls for).

Affected: `handle_register`, `handle_invoke`, `handle_respond`, `handle_stream`, `handle_health`, `handle_introspect`, `handle_admin_*` (state, stream, manifest, reload, processes, spawn, stop, restart, node_status, ui_state), plus app glue (`make_app`, `amain`, `main`, `consume`, `on_shutdown`, `emit_supervisor_event`, `emit_envelope`, `audit`, `relationships_for`, `load_manifest`, `CoreState` class).

### Type-hint gaps
None found in `core/` or `node_sdk/` public functions. Both modules have full annotations including return types. Nothing to fix.

### Behaviour / refactors
Out of scope per task brief ("don't refactor logic"). The aiohttp `NotAppKeyWarning` (144 warnings, `core/core.py:897` and `:922`) is a pre-existing deprecation we should fix in a separate pass.

---

## Layering observations (FLAG, not fix)

Re-stated against `notes/PROTOCOL_CONSTRAINT.md`:
- `core/` (protocol) — **clean.** No imports of `nodes.*` or `dashboard.*`. No string references to specific node IDs. The one mention of "kanban, voice, dashboard" in `core/supervisor.py:80` is a docstring callout *enforcing* the constraint, not a leak.
- `node_sdk/` (protocol) — **clean.** No node-specific or dashboard references.
- `core/__init__.py` — empty file. Fine.
- `nodes/__init__.py` — examined; no protocol-layer leakage either way.

**No layering violations were found that need cleanup.** If any are introduced in Wave 2/3 work, this scan is reproducible — see "Methodology" above.

One gentle suggestion (not a violation): the substring `dashboard` appears in the supervisor docstring at line 80 as an example. If we ever rename the dashboard node, this docstring will quietly drift; consider rephrasing to "any node type" instead of naming examples. **Defer** — too minor to bundle here.

---

## Files changed

```
core/supervisor.py
node_sdk/__init__.py
node_sdk/sse.py
nodes/kanban_node/kanban_node.py
nodes/nexus_agent/agent.py
nodes/nexus_agent_isolated/agent.py
nodes/voice_actor/audio_io.py
nodes/voice_actor/realtime_client.py
notes/cleanup_pass.md  (new)
```

All changes are non-functional: import removals + one-line docstrings.
