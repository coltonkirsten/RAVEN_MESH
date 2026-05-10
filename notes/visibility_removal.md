# Visibility Removal — Execution Note

**Branch:** `simplify-raven` (created from `main` for this task)
**Date:** 2026-05-10
**Reference:** `notes/2026-05-10_morning_review.md` §9 — Colton's decision to delete UI-visibility from Core entirely.

## Outcome

Protocol-layer leak removed. `/v0/admin/node_status`, `/v0/admin/ui_state`, and the `node_status` field on `CoreState` no longer exist. Dashboard's "UI Visibility" tab and API binding gone. Helper-side `report_status` and the two ad-hoc POSTs from the nexus agents deleted. The `ui_visibility` *tool surface* (and its two schemas) remains as opt-in node-level functionality.

Full test suite: **144 passed**.

## Files touched

### Protocol (core)
- `core/core.py` —
  - dropped two docstring lines (lines ~14-15)
  - removed `state.node_status` field init
  - removed `node_status` from `/v0/admin/state` payload
  - deleted `handle_admin_node_status` and `handle_admin_ui_state`
  - removed the two `app.router.add_*` registrations

### Dashboard (caller)
- `dashboard/src/App.tsx` — removed `UiVisibility` page, `EyeOff` import, and `"visibility"` page key
- `dashboard/src/lib/api.ts` — removed `getUiState()`
- `dashboard/src/lib/types.ts` — removed `NodeStatus` type and `node_status` field on `AdminState`
- `dashboard/src/pages/UiVisibility.tsx` — **deleted**

### Nodes (callers)
- `nodes/ui_visibility.py` — deleted `report_status`, `admin_token`, and `DEFAULT_ADMIN_TOKEN`. Dropped `aiohttp`/`os`/`asyncio` imports that became unused. Removed the `core_url` parameter from `make_handler` since the handler no longer talks to Core.
- `nodes/approval_node/approval_node.py` — dropped `report_status` import + post-serve call; updated `make_visibility_handler` call to drop `core_url`.
- `nodes/human_node/human_node.py` — same
- `nodes/voice_actor/voice_actor.py` — same
- `nodes/webui_node/webui_node.py` — same
- `nodes/nexus_agent/agent.py` — deleted `_report_visibility` method and the `asyncio.create_task(...)` line in `handle_ui_visibility`
- `nodes/nexus_agent_isolated/agent.py` — same

### Tests
- `tests/test_admin.py` — deleted `test_admin_node_status_and_ui_state` (the only test that exercised the deleted endpoints).

### Untouched (per task)
- `schemas/ui_visibility.json` and `schemas/kanban_ui_visibility.json` — kept as opt-in node-level surface schemas.
- All manifests under `manifests/` — they still declare `ui_visibility` surfaces and edges; that is opinionated-layer config and not a protocol concern.
- `nodes/kanban_node/kanban_node.py` — already had a self-contained `ui_visibility` handler that never touched Core's admin API, so no change needed.

## Verification

```
$ grep -rn 'node_status\|ui_state' core/ node_sdk/ dashboard/src/ nodes/ tests/
(no matches)

$ grep -rn 'ui_visibility' core/ node_sdk/ dashboard/src/
(no matches)
```

Remaining `ui_visibility` references in `nodes/` and `tests/` are either (a) tool-surface declarations, (b) handlers for that surface, or (c) tests of those handlers — all opinionated-layer, none protocol.

```
$ python3 -m pytest -q
144 passed in 21.10s
```

## Surprises / notes

1. **Caller branch was wider than §9 listed.** §9 specifically named Core, the helper, and the dashboard. In practice four nodes (approval/human/voice_actor/webui) had post-`serve` `report_status` calls in addition to the handler-side reports through the helper, and both `nexus_agent` and `nexus_agent_isolated` had hand-rolled `_report_visibility` methods that bypassed the helper entirely. All seven sites had to be cleaned, not just the helper.

2. **`make_handler` signature changed.** Dropped `core_url` since the handler no longer needs it. All four call sites updated.

3. **Dashboard build artifact (`dashboard/dist/assets/index-*.js`) is stale** — still contains the old `getUiState`/`UiVisibility` code. It is not git-tracked, so it won't be committed, but anyone serving the existing prebuilt bundle will see the dead tab until a rebuild. Not in scope to rebuild here; flagging for awareness.

4. **Branch creation:** `simplify-raven` did not exist in this repo; created it from `main` since the task said "work on this branch, do not switch." The current `simplify-raven` branch in the parent `raven` repo is unrelated.

5. **The `nodes/kanban_node` ui_visibility tool is self-contained** — it doesn't use `nodes/ui_visibility.py` at all (it has its own handler that flips a local flag) and never reported to Core. No edits needed there.

6. **`docs/`-style notes left as-is.** Files under `notes/` (`migration_path.md`, `operational_playbook.md`, `wave_1_critique.md`, `docs_drafts/PROTOCOL.md`, etc.) still mention the deleted endpoints. Updating the docs corpus is a separate cleanup; the task scope was code + tests.
