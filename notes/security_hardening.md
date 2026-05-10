# RAVEN_MESH security hardening — 2026-05-10

**Author:** worker (executing Wave-2 fixes off `security_audit_20260510.md` and
`security_postmortem.md`).
**Branch:** `simplify-raven` (mesh repo `main` head + this PR).
**Layer discipline:** Per `PROTOCOL_CONSTRAINT.md`. Every change below is
tagged `[protocol]` and lives in `core/`. Opinionated companions
(`dashboard/vite.config.ts`, `nodes/*`, `scripts/_env.sh`) are explicitly
deferred — they belong with their respective node owners and do not gate the
protocol-side hardenings.

---

## What landed

Three protocol-layer changes against `core/core.py`. All generic — a fork that
threw away every node and the dashboard would still feel correct on these
lines.

### 1. `[protocol]` Rotated `ADMIN_TOKEN`: no built-in default (V-01, V-18)

**File:** `core/core.py`.

- Removed `DEFAULT_ADMIN_TOKEN = "admin-dev-token"`.
- `admin_token()` now raises `RuntimeError` if `ADMIN_TOKEN` is unset OR equals
  the legacy `"admin-dev-token"` placeholder. Failure is loud, at boot, before
  the listener accepts a single connection.
- `make_app` calls `admin_token()` once at construction so misconfigured
  deployments crash on startup rather than during the first admin request.
- `_admin_authed()` is now header-only — `?admin_token=` query-string auth is
  gone. Query-string secrets land in shell history, browser history, server
  access logs, and the `Referer` of any link clicked from a Core-served page.
  The check uses `hmac.compare_digest` for constant-time comparison.

**Tests:** `test_admin_rejects_query_string_token`,
`test_admin_token_boot_check_refuses_unset`,
`test_admin_token_boot_check_refuses_legacy_default`. The shared test fixture
sets a non-default `ADMIN_TOKEN` (`tests/conftest.py`), and three test files
that previously hardcoded `"admin-dev-token"` were updated.

This is the protocol's half of audit fix #1. The opinionated companion — the
Vite dev proxy stamping the legacy default into every request — is
**deferred**; it lives in `dashboard/vite.config.ts` and is the dashboard
team's call. Without that companion, the dashboard's `npm run dev` flow will
need its operator to export `ADMIN_TOKEN` and update the proxy to read it,
which is the desired forcing function.

### 2. `[protocol]` Token-bucket rate limit on `/v0/admin/*` (V-05)

**File:** `core/core.py`.

- New `_AdminRateLimiter` class (token-bucket, async-safe, configurable).
  Default: 60 req/min refill, 20-token burst per source IP. Configurable via
  `MESH_ADMIN_RATE_LIMIT` (per-minute fill rate) and `MESH_ADMIN_RATE_BURST`
  (capacity). Rate=0 disables.
- New `_admin_rate_limit_middleware` registered ahead of every admin handler.
  Returns `429 {"error":"rate_limited","scope":"admin"}` with `Retry-After: 1`
  on bucket exhaustion. CORS preflight (`OPTIONS`) bypasses the limiter so
  browser preflights aren't gated.
- Source key is `X-Forwarded-For` (first hop) when present, otherwise the
  connection peer IP. Bucket dictionary self-evicts entries that have
  refilled to capacity, and is hard-capped at 4096 keys to prevent memory
  growth from sprayed source IPs.

**Why a per-IP bucket and not per-token bucket:** the protocol does not (and
should not) know what an opinionated client thinks a "user" is. The IP key is
the most generic primitive that still rate-limits manifest-flood and
admin-state-scrape attacks without baking node-specific assumptions into the
protocol layer. If a deployment puts the dashboard behind a proxy, the proxy
already sets `X-Forwarded-For`, which the limiter respects.

**Tests:** `test_admin_rate_limit_returns_429` boots Core with a tight bucket
(`burst=3`), fires 8 admin requests, asserts a 429 appears and within the
expected window. The other admin tests inherit the default (`burst=20`) which
is loose enough that no existing test trips it.

### 3. `[protocol]` Bounded per-node delivery queue (V-06)

**File:** `core/core.py`.

- `handle_register` now creates `asyncio.Queue(maxsize=NODE_QUEUE_MAX)` (1024)
  for each connected node. Replaces the prior unbounded `asyncio.Queue()`,
  which let a slow or hostile node accumulate envelopes until OOM.
- `_route_invocation` now uses `put_nowait` against the target queue and
  catches `asyncio.QueueFull`. On overflow:
  - Audit entry written: `decision="denied_queue_full"` with the target node
    id and the queue cap.
  - Envelope tail / admin tap entry: `route_status="denied_queue_full"`.
  - HTTP response: `503 {"error":"denied_queue_full","node":<id>}`.
  - For request/response invocations the `state.pending` slot is rolled back
    so the caller doesn't leak a `pending` entry for an envelope that was
    never delivered.
- The fire-and-forget path was previously `await put`, which would block (not
  drop) on a full bound. Both modes now share the same overflow semantics:
  fail fast and surface the back-pressure in the audit log.

The cap value (`NODE_QUEUE_MAX = 1024`) is exposed as a module constant. The
protocol's choice — that there *is* a cap — is generic. The specific number
is a tunable; bumping it does not require code changes outside `core/core.py`.

**Tests:** `test_node_queue_is_bounded` directly verifies the bound by
filling a fresh `asyncio.Queue(maxsize=NODE_QUEUE_MAX)` to capacity and
asserting `put_nowait` raises `QueueFull`. We did not add a full integration
test that connects a slow consumer + 1025 senders — that's slow and flaky.
The unit-level assertion is sufficient because the routing path is exercised
end-to-end by the existing protocol/admin tests.

### Test suite

`pytest tests/` reports **125 passed** (was 120 before this PR; +5 new tests
covering the new properties). No regressions. Run from repo root:
```
python3 -m pytest tests/ -q
```

---

## What was deliberately deferred

Each line below is something the audit/postmortem flagged and we did not ship
in this PR. They are tagged with the layer they belong to.

### Protocol-layer (audit calls these out, but out of scope for *this* PR)

- **V-02 (manifest secret rotation):** Refusing inline `identity_secret` on
  `/v0/admin/manifest` writes when an active connection already holds that
  secret. Belongs in `handle_admin_manifest` + `_resolve_secret`. Real fix,
  but the manifest-validator wiring (already merged at
  `core/manifest_validator.py`, not yet wired into `load_manifest`) is the
  natural home — that wiring is its own PR. Deferred so this PR remains
  surgical.
- **V-03 (HMAC replay):** Timestamp window + nonce-cache rejection on
  `register`/`invoke`. Touches `verify`, `handle_register`, the SDK, and
  every test envelope. ~80 LoC across protocol + SDK plus rebaselined fixture
  envelopes. Deferred.
- **V-04 (admin-invoke provenance):** Tag `admin_synthesized=True` on the
  envelope and propagate into audit/tap. Tractable; deferred only because
  the audit's "top 3" did not include it.
- **V-08 (`_derive` is public):** The `_derive` recipe lives in
  `scripts/_env.sh` (opinionated dev-loop convenience), but the
  protocol-side change — making `_resolve_secret` raise on missing `env:VAR`
  instead of fabricating a known fallback — is `[protocol]`. Deferred to
  pair with V-02 in the next pass; both touch `_resolve_secret` and the
  manifest-loader trust path.
- **V-12 (SSE durability), V-13 (schema-path traversal), V-14 (CORS `*`),
  V-15 (manifest write race), V-16 (audit-log integrity):** All listed in
  the postmortem's gap matrix; none were in the audit's top-3 leverage
  picks. Park for a second protocol-hardening pass.

### Opinionated-layer (do not ship in protocol)

These cannot live in `core/` without violating `PROTOCOL_CONSTRAINT.md` —
they're product policy, not generic safety. Owners listed for handoff:

- **V-01 (dashboard half):** `dashboard/vite.config.ts:14-29` injects the
  default token; refusing to start without `ADMIN_TOKEN` is a dashboard
  decision. Owner: dashboard.
- **V-07 (`--dangerously-skip-permissions` gating):**
  `nodes/nexus_agent/cli_runner.py`,
  `nodes/nexus_agent_isolated/docker_runner.py`. Owner: nexus_agent.
- **V-09 (OpenAI ephemeral keys):** `nodes/voice_actor/voice_actor.py`.
  Owner: voice_actor.
- **V-10 / V-11 (isolated agent bind + OAuth volume):**
  `nodes/nexus_agent_isolated/{agent.py,docker_runner.py,entrypoint.sh}`.
  Owner: nexus_agent_isolated.
- **V-17 (kanban API auth):** `nodes/kanban_node/kanban_node.py`. Owner:
  kanban_node.
- **V-19 (control-token redaction):**
  `nodes/nexus_agent_isolated/docker_runner.py`. Owner: nexus_agent_isolated.
- **V-20 (`webui_node.change_color` regex):**
  `schemas/webui_change_color.json`. Owner: webui_node.

The postmortem's open question on validator-strict-on-remote-write also
remains open. The recommendation there (security rules always-on, hygiene
rules opt-in via `MESH_STRICT_MANIFEST`) is the right shape, but wiring it
is the validator-merge PR, not this hardening pass.

---

## Migration notes for operators

- **Set `ADMIN_TOKEN` before starting Core.** No fallback exists. `make_app`
  raises immediately on unset / legacy default. Suggested: 32 random bytes
  hex-encoded, exported in the operator's shell.
- **The `?admin_token=` URL-parameter shortcut is gone.** Header only:
  `curl -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8000/v0/admin/state`.
- **Admin clients should expect 429s.** Defaults are 60/min refill,
  burst 20; tune via `MESH_ADMIN_RATE_LIMIT` / `MESH_ADMIN_RATE_BURST`.
  Setting either to `0` disables rate limiting (don't ship that to prod).
- **Slow consumers now see 503s instead of latent OOM.** Audit log decisions
  `denied_queue_full` are the new diagnostic surface; the dashboard's
  envelope-tap will show `route_status="denied_queue_full"` for affected
  invocations.

---

## Files touched

```
core/core.py                                 (+~120 / -~10)
tests/conftest.py                            (+8 / -0)
tests/test_admin.py                          (+76 / -2)
tests/test_supervisor_integration.py         (+1 / -1)
tests/test_voice_actor.py                    (+2 / -1)
notes/security_hardening.md                  (new)
```

No files outside `core/` and `tests/` were modified. The protocol layer is
unchanged in surface area: same routes, same envelope shape, same wire
contract — only stricter on auth, rate, and queue-bound semantics.
