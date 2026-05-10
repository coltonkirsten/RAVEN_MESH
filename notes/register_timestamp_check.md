# /v0/register timestamp-window check

**Date:** 2026-05-10
**Branch:** simplify-raven
**Layer:** protocol (generic; applies to every node that registers)

## Threat model recap

- `/v0/register` requires a valid HMAC signature against the node's secret.
  An attacker without the secret cannot forge a register envelope.
- An attacker who captures a legitimate register envelope on the wire (or
  from logs, etc.) can replay it. Each replay kicks the legitimate node's
  current session and creates a new `session_id` that is returned to the
  attacker. The attacker still cannot do anything useful with that
  `session_id` because they cannot sign subsequent envelopes — they lack
  the secret.
- Net effect of replay: **denial-of-service**, not privilege escalation.
  The attacker can repeatedly disconnect the legitimate node.
- Before this change, a captured envelope could be replayed forever. After
  this change, the attacker is reduced to a live MITM window of
  `MESH_REPLAY_WINDOW_S` (default 60s, bounds [5, 300]).

## Implementation choice: sibling helper, not a flag on `check_replay`

`a4eaa15` added `state.check_replay(env)` which combines timestamp-window
validation and protocol-wide nonce-LRU dedup. `/v0/invoke` and `/v0/respond`
both use it.

Two ways to extend it for register:

1. **Add a mode flag** — e.g. `check_replay(env, *, nonce=False)`. Risks
   regressing the two endpoints that already depend on the combined
   behaviour. Every future change to the helper now has to think about two
   modes.
2. **Add a sibling helper** — `state.check_timestamp_only(env)`. Zero impact
   on the existing `check_replay` callers. The two helpers can evolve
   independently.

I chose option 2. The sibling helper:

- Returns `(ok, drift_s)` instead of `(ok, error_code)` so we can log the
  observed drift.
- Uses the same `MESH_REPLAY_WINDOW_S` window as `check_replay`.
- Does not touch the nonce LRU.

Why no nonce check on register: the register envelope schema today is
`{node_id, timestamp, signature}` — no unique id field. Adding nonce-LRU
would need an SDK change to start sending an id, and then a coordinated
rollout. Out of scope here.

## Code touched

- `core/core.py`
  - Added `CoreState.check_timestamp_only(env)` next to `check_replay`.
  - In `handle_register`: after the existing HMAC `verify(...)` check and
    before the connection-takeover block, call `check_timestamp_only`.
    On failure, log at INFO with drift + window + node_id and return 401
    `{"error": "stale_register", "reason": "timestamp outside replay window"}`.
- `tests/test_register_replay.py` — new file, 5 cases (below).
- `tests/test_protocol.py::test_step_10_external_language_node` — replaced
  the literal `"timestamp": "now"` placeholder with `now_iso()` so the
  manual-register example produces a parseable timestamp.

## Test cases (`tests/test_register_replay.py`)

1. `test_register_accepts_fresh_timestamp` — register with `now_iso()`
   returns 200 and a `session_id`.
2. `test_register_accepts_within_window` — register with timestamp 30s
   old (default window 60s) returns 200.
3. `test_register_rejects_stale_timestamp` — with `replay_window_s=5`,
   register with timestamp 70s old returns 401 `stale_register`.
4. `test_register_rejects_missing_timestamp` — register with no
   `timestamp` field returns 401 `stale_register` (fail closed).
5. `test_register_replay_within_window_still_accepted` — replaying the
   same valid register envelope a few ms later succeeds (documented gap;
   each call returns a fresh `session_id` because of connection takeover).

Full suite: 157 passing (was 152, now 152 + 5 new = 157).

## What is still ungated, and why

- **In-window replay of a captured register envelope.** An attacker who
  captures a register envelope on the wire can replay it within
  `MESH_REPLAY_WINDOW_S`. They get a fresh `session_id` they cannot use,
  but the legitimate node's session is kicked.
- **Why we did not close it in this PR:** closing it requires the SDK to
  add a unique id to the register envelope, and Core to dedup it via the
  same nonce LRU `check_replay` already maintains. That is an SDK
  protocol change and a coordinated rollout — out of scope for a
  protocol-only patch on Core. Deferred to the next SDK envelope-id bump.

## Protocol-vs-opinionated check

This change touches `core/core.py` only. It does not encode any
node-shaped assumption (no special-casing for kanban, voice_actor,
dashboard, etc.) and applies uniformly to every node that registers. The
log line includes `node_id` because that is structured operator-facing
output — it does not change protocol semantics. Substitution test
passes: a fork of RAVEN_MESH with every node deleted would still want a
timestamp-window check on `/v0/register`.
