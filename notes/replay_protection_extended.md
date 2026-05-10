# HMAC replay protection — extended to /v0/respond + configurable window

Date: 2026-05-10
Branch: simplify-raven
Worker: 14d6a02b
Layer: protocol

## Design concern surfaced (READ FIRST)

The morning review §11 stated:

> "Replay protection is wired on /v0/register and /v0/invoke (timestamp ±60s
> + nonce LRU) but NOT /v0/respond."

**This was incorrect.** No replay protection existed in `core/core.py` before
this change. The audit doc `notes/security_audit_20260510.md` V-03 identified
the gap; the proposal `notes/security_patches/03_hmac_replay_protection.patch`
sketched the fix and explicitly carries the line `# DO NOT APPLY — proposal
for review.` It was never applied.

I did not silently restructure existing security code, because there was none
to restructure. The task was to "extend" protection to `/v0/respond`; the
phrase "same code path, same enforcement" became the design constraint
instead — one helper, applied uniformly to every envelope-routing endpoint
that owns a `msg_id`.

## What changed

### Pattern: per-handler call to a shared helper on `CoreState`
Not middleware, not a route allowlist. The check is `state.check_replay(env)`
called inside `_route_invocation` (the shared code path used by
`/v0/invoke`) and at the top of `handle_respond` (`/v0/respond`).
The helper owns one `OrderedDict` keyed by envelope `id` — **a single
protocol-wide LRU**, not per-route. This preserves the invariant that nonces
are unique across the protocol; if a future endpoint joins the gated set, it
shares the same nonce space automatically.

Both call sites enforce identical semantics:
- `stale_or_missing_timestamp` → HTTP 401
- `replay_detected` (id reuse) → HTTP 409
- Window: `state.replay_window_s`, loaded once per Core instance from env.

### Endpoints gated
- `/v0/invoke` (via `_route_invocation`, only when `signature_pre_verified`
  is False — admin-synthesized envelopes skip the check, since Core
  generates them in-process and the LRU would just collect noise).
- `/v0/respond`.

### Endpoints NOT gated (intentional, surfaced for follow-up)
- `/v0/register` — the body is `{node_id, timestamp, signature}` with no
  unique id. Adding replay protection there requires a real protocol change
  (an SDK-emitted nonce field on register) which is out of scope for this
  task. The hole called out in the audit (V-03 "replay register forever")
  remains open. Recommend a separate worker for that — it touches the SDK
  and any external-language node's register flow.

### Env-var: `MESH_REPLAY_WINDOW_S`
- Read in `_load_replay_window_s()` (`core/core.py`, near the module-level
  constants). Loaded once when `CoreState.__init__` runs, then stored on
  `state.replay_window_s` for the rest of the process lifetime.
- Bounds: `[REPLAY_WINDOW_MIN_S=5, REPLAY_WINDOW_MAX_S=300]`.
- Default: `REPLAY_WINDOW_DEFAULT_S=60`.
- Out-of-range and non-integer inputs log a `WARNING` on `mesh.core` and
  fall back to clamp/default. The operator cannot disable replay
  protection by setting an obscene value, nor lock the protocol out by
  setting zero.

### Sample WARNING output
```
WARNING mesh.core: MESH_REPLAY_WINDOW_S=0 below floor; clamped to 5s
WARNING mesh.core: MESH_REPLAY_WINDOW_S=9999 above ceiling; clamped to 300s
WARNING mesh.core: MESH_REPLAY_WINDOW_S='not_a_number' is not a valid int; falling back to default 60s
```
(Captured manually with `MESH_REPLAY_WINDOW_S=<value> python3 -c '...'`.)

## Tests added

`tests/test_replay_protection.py` (8 cases, all pass):

Env-var loader:
- `test_replay_window_default_when_unset` — unset → 60.
- `test_replay_window_honors_in_range_value` — 30 → 30.
- `test_replay_window_clamps_above_ceiling` — 10000 → 300, WARNING emitted.
- `test_replay_window_clamps_below_floor` — 0 → 5, WARNING emitted.
- `test_replay_window_falls_back_on_garbage` — `not_a_number` → 60, WARNING emitted.

End-to-end replay gating:
- `test_respond_rejects_replayed_envelope_within_window` — capture a real
  `/v0/respond` envelope, replay it ~100ms later → 409 `replay_detected`.
- `test_respond_rejects_envelope_outside_window` — hand-build an envelope
  with a timestamp 70s in the past → 401 `stale_or_missing_timestamp`.
- `test_invoke_rejects_replayed_envelope` — hand-build a signed invocation,
  POST it twice → first 200, second 409 `replay_detected`.

Test fix in `tests/test_protocol.py::test_step_10_external_language_node`:
the hand-rolled response envelope used `"timestamp": "now"`, which now fails
the freshness check. Switched to `now_iso()`. The same test's hand-rolled
register body still uses the literal string `"now"` because `/v0/register`
is not gated — leaving that line as-is keeps the test honest about which
endpoints currently enforce freshness.

Full suite: `python3 -m pytest -q` → **152 passed**.

## Files touched

- `core/core.py` — env-var loader, helper, `CoreState` fields, gating in
  `_route_invocation` + `handle_respond`.
- `tests/test_replay_protection.py` — new.
- `tests/test_protocol.py` — single-line fix for `now_iso()`.

## Constraint check (PROTOCOL_CONSTRAINT.md)

Replay protection is a wire-protocol property: any signed envelope that
carries a `timestamp` and a unique `id` is replayable, and Core's job is to
reject the replay. No node-shaped justification appears in the code or the
commit message. The fix would still be the right fix in a fork that threw
away every node and the dashboard.
