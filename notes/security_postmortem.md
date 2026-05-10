# RAVEN_MESH security postmortem — 2026-05-10

**Author:** worker (synthesis of `security_audit_20260510.md` + `manifest_validation_design.md`)
**Scope:** `core/`, `node_sdk/`, `nodes/`, `scripts/`, `manifests/`, `dashboard/` at HEAD of `simplify-raven`.
**Layer discipline:** Per `PROTOCOL_CONSTRAINT.md`. Every mitigation below is tagged
`[protocol]` (lives in `core/`, `node_sdk/`, envelope schema, manifest schema, `/v0/admin/*`)
or `[opinionated]` (lives in `nodes/*`, `dashboard/`, `scripts/`, `manifests/full_demo.yaml`).
The protocol must remain unopinionated — when a fix is a *policy choice for our specific
deployment*, it goes in the opinionated layer; when it is a *generic safety property of
the building block*, it goes in the protocol.

---

## 1. What this postmortem is

The audit enumerated 20 issues across the wire protocol, the admin API, the manifest
loader, the supervisor, and several specific nodes. The validator-design note, written
in parallel, proposed wiring a manifest validator into `load_manifest`. Both surfaces
share a single root concern: **`load_manifest` is the trust root of the mesh — it
decides which nodes exist, what they can route to, and what secret signs their packets
— and today it accepts arbitrary input from a remote admin write with almost no
checking.** Several individually-flagged issues collapse onto that one root.

This document is the unified threat model, the gap matrix, and three concrete diffs.

## 2. Threat model

Adversary classes the design assumes (loopback-only v0, intentional cloud out of scope):

| # | Class | Assumed capability |
|---|---|---|
| A | Same-host hostile process | Read RAVEN_MESH source; reach `127.0.0.1:8000`; `tcpdump -i lo0`; read user shell env |
| B | Browser tab on the user's Mac | Run JS; issue `fetch(..., { mode: 'no-cors' })` to any same-host port |
| C | LAN neighbour | Reach any `0.0.0.0`-bound port; not on loopback |
| D | Single-frame wire capture | Has one valid HMAC envelope but no secret |
| E | Compromised dependency | Runs as the Core user inside the supervisor |

Out of scope: kernel rootkits; physical access; cloud network; supply-chain attacks
that pre-date the audit.

## 3. Attack surface inventory

| Entrypoint | Layer | Adversaries it gates |
|---|---|---|
| `POST /v0/register` (HMAC body) | protocol (`core/core.py:192`) | A, D |
| `POST /v0/invoke` (HMAC body) | protocol (`core/core.py:232`) | A, D |
| `GET /v0/stream` (SSE) | protocol (`core/core.py:355`) | A |
| `POST /v0/admin/*` (token) | protocol (`core/core.py:511-700`) | A, B |
| Vite dev proxy `/api/admin/*` | opinionated (`dashboard/vite.config.ts:14-29`) | B |
| Manifest YAML on disk | protocol surface, opinionated content | E |
| `metadata.runner.cmd` → `/bin/sh -c` | protocol gate, opinionated content | A, B (via admin), E |
| Per-node HMAC secret (env or `_derive`) | protocol contract, opinionated default | A |
| `kanban_node` HTTP API on `127.0.0.1:8805` | opinionated (`nodes/kanban_node`) | A, B |
| `nexus_agent_isolated` control on `0.0.0.0:*` | opinionated (`nodes/nexus_agent_isolated`) | C |
| `voice_actor` `OPENAI_API_KEY` from `.env` | opinionated (`nodes/voice_actor`) | A, E |

The protocol is responsible for the first six rows; it must be defensible *regardless
of what nodes someone builds on top*. Rows 7–11 are this product's choices and must be
hardened in their own modules — not by leaking node-specific code into core.

## 4. Findings × mitigations × layer

Severity from the audit; mitigation phrased as the property the fix establishes; the
layer column says where the fix must live.

| ID | Severity | Property the fix establishes | Layer | Where it lands |
|---|---|---|---|---|
| V-01 | CRITICAL | Admin API requires non-default token; rejects query-string and `no-cors` cross-origin POSTs; refuses `metadata.runner.cmd` unless explicitly opted in | `[protocol]` for the auth/Origin/runner-cmd gate | `core/core.py:_admin_authed`, `core/supervisor.py:make_script_resolver` |
| V-01 (proxy injection) | CRITICAL | Dev proxy refuses to inject default token | `[opinionated]` | `dashboard/vite.config.ts` |
| V-02 | CRITICAL | Remote manifest writes cannot rotate `identity_secret` of an active node | `[protocol]` | `core/core.py:handle_admin_manifest` + `_resolve_secret` |
| V-03 | CRITICAL | Envelopes carry a fresh nonce + bounded timestamp; replays rejected | `[protocol]` | `core/core.py:verify`, `node_sdk/__init__.py:48` |
| V-04 | HIGH | `/v0/admin/invoke` envelopes are tagged `admin_synthesized=True` in audit + tap | `[protocol]` | `core/core.py:handle_admin_invoke` |
| V-05 | HIGH | Token-bucket rate limit on admin namespace and per-`from_node` invoke | `[protocol]` | `core/core.py` middleware |
| V-06 | HIGH | Per-node delivery queue is bounded; `QueueFull` → 503 + audit | `[protocol]` | `core/core.py:handle_register` |
| V-07 | HIGH | `--dangerously-skip-permissions` is gated by `MESH_ALLOW_DANGEROUS=1` | `[opinionated]` | `nodes/nexus_agent/cli_runner.py`, `nodes/nexus_agent_isolated/docker_runner.py` |
| V-08 | HIGH | Per-machine random secret master replaces `sha256("mesh:<id>:dev")`; `env:VAR` missing fails loud | `[protocol]` for `_resolve_secret`; `[opinionated]` for `scripts/_env.sh` defaults | `core/core.py:_resolve_secret`, `scripts/_env.sh` |
| V-09 | HIGH | OpenAI ephemeral session tokens; concurrent-session cap | `[opinionated]` | `nodes/voice_actor/voice_actor.py` |
| V-10 | HIGH | Control server bound to `127.0.0.1` or UDS, not `0.0.0.0` | `[opinionated]` | `nodes/nexus_agent_isolated/agent.py` |
| V-11 | HIGH | Short-lived OAuth tokens in container; `~/.claude/auth/` on tmpfs | `[opinionated]` | `nodes/nexus_agent_isolated/{docker_runner.py,entrypoint.sh}` |
| V-12 | MEDIUM | Per-stream sequence + `Last-Event-ID` replay buffer | `[protocol]` | `core/core.py:handle_stream`, `node_sdk/__init__.py:_stream_loop` |
| V-13 | MEDIUM | Manifest schema paths must resolve under `manifest_dir` | `[protocol]` | `core/core.py:load_manifest` (or via the new validator) |
| V-14 | MEDIUM | Admin CORS is allow-listed, not `*` | `[protocol]` | `core/core.py:_cors_middleware` |
| V-15 | MEDIUM | Manifest write+load+rollback is serialised | `[protocol]` | `core/core.py:handle_admin_manifest` |
| V-16 | MEDIUM | Audit log rotates and HMAC-chains | `[protocol]` | `core/core.py:audit` |
| V-17 | MEDIUM | Kanban HTTP API requires a same-origin gate or per-load CSRF token | `[opinionated]` | `nodes/kanban_node/kanban_node.py` |
| V-18 | LOW | Admin API rejects query-string token | `[protocol]` | `core/core.py:_admin_authed` |
| V-19 | LOW | `NEXUS_AGENT_CONTROL_TOKEN=` redacted in inspector logs | `[opinionated]` | `nodes/nexus_agent_isolated/docker_runner.py` |
| V-20 | LOW | `webui_node.change_color` validated against hex regex | `[opinionated]` (schema lives with the node) | `schemas/webui_change_color.json` |

A few mitigations cross layers. V-01 is the clearest example: the *protocol* must reject
unsigned cross-origin admin POSTs and stop honouring inline shell commands; the *dashboard*
must independently stop injecting the default token in its Vite proxy. Either fix alone
leaves the other half of the path open. Both halves must ship.

## 5. Manifest validation as a security primitive

The validator note (already merged at `core/manifest_validator.py` with 19 tests) is
billed as "schema hygiene", but three of its checks are load-bearing for the threat
model — and three more *should* be added for the same reason:

**Already in the validator (covers parts of V-01/V-02 by accident):**
- Reject relationships pointing at undeclared nodes — closes a class of "ghost edge"
  attacks where a manifest write declares a relationship from a node that never connects
  but whose secret is rotated by `_resolve_secret`.
- Reject duplicate node ids (would otherwise let an attacker shadow a real node by
  declaring a second entry that overwrites in dict-iteration order).
- Reject reserved id `core` (prevents a node from claiming the core admin namespace
  if/when `core.*` becomes a real surface family).

**Should be added; closes V-13 and tightens V-01/V-02:**
- `error: schema path escapes manifest_dir` — strictly equivalent to the V-13 mitigation
  but lives in pure validator code, where it's testable without spinning up Core.
- `error: identity_secret is inline (not env:)` when validator is invoked in
  `remote_write` mode — the V-02 fix expressed as a validator rule. The validator gains
  an `origin` parameter (`"local" | "remote_write"`); only `local` permits inline
  secrets.
- `error: metadata.runner.cmd is set` when validator is invoked in `remote_write` mode
  and `MESH_ALLOW_INLINE_RUNNERS!=1` — the V-01 fix expressed as a validator rule.

Wiring decisions:
- Stage 1 of the validator design (warnings-only, strict opt-in) is the right default
  for the *static* validation rules. **It is not the right default for the
  security-critical rules above** — those should always raise on `remote_write`,
  regardless of `MESH_STRICT_MANIFEST`. The validator should expose two thresholds:
  hygiene (opt-in strictness) and security (always strict on remote writes).

This keeps the validator a `[protocol]` artifact: it encodes generic safety properties
of the manifest format. The specific list of "what `nexus_agent` declares" remains
`[opinionated]` content inside `manifests/full_demo.yaml`.

## 6. Gap matrix

What is hardened today, what's partially mitigated, what's open. Use this for
prioritisation, not for measuring "done":

| Concern | Hardened | Partial | Open |
|---|---|---|---|
| Admin auth | Token check exists | Default token, query-string accepted, no Origin check, dev-proxy injection | V-01, V-14, V-18 |
| Manifest as RCE | YAML parse, schema parse | No path-escape check, inline runner.cmd allowed, secrets rotatable on remote writes | V-01, V-02, V-13 |
| Wire integrity | HMAC of canonical body | Timestamp present but unchecked; no nonce; no replay window | V-03 |
| Trust root (secrets) | `env:VAR` indirection contract | `_derive` is public; missing env silently fabricates a known fallback | V-08 |
| DoS / back-pressure | `_admin_streams` capped | Per-node `_streams` queue unbounded; no rate limit on admin or invoke | V-05, V-06 |
| Provenance / audit | Audit log per route decision | Spoofed `from_node` looks identical to legitimate sender; no log integrity | V-04, V-16 |
| Stream durability | EventSource reconnect implemented in SDK | Reconnect drops queued envelopes; no per-stream sequencing | V-12 |
| Specific-node attack surface | Dotenv loader exists | `--dangerously-skip-permissions` ungated; OpenAI key long-lived; isolated agent on `0.0.0.0`; OAuth token persisted in named volume; kanban API unauthed | V-07, V-09, V-10, V-11, V-17, V-19, V-20 |

Read the matrix as: *the protocol's trust root and back-pressure are the two areas where
the most work is still needed*. The opinionated-layer issues are numerous but each
isolated to its node — they parallelise across owners.

## 7. Top 3 highest-leverage fixes — concrete diffs

Same picks as the audit, restated with the layer tag and a sketched diff. Diffs are
illustrative; full PR will need test updates and runtime hooks.

### Fix 1 — Lock down the admin trust path (V-01 + V-02 + V-14 + V-18)

**Layer: `[protocol]`** (the auth and Origin gate live in core); paired with a
**`[opinionated]`** dashboard change.

```diff
--- a/core/core.py
+++ b/core/core.py
@@ -38,11 +38,12 @@
 ENVELOPE_TAIL_MAX = 200
-DEFAULT_ADMIN_TOKEN = "admin-dev-token"
+ALLOWED_ADMIN_ORIGINS = frozenset(
+    s for s in os.environ.get("MESH_ALLOWED_ADMIN_ORIGINS", "").split(",") if s
+)
@@
-def admin_token() -> str:
-    return os.environ.get("ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)
+def admin_token() -> str:
+    tok = os.environ.get("ADMIN_TOKEN")
+    if not tok or tok == "admin-dev-token":
+        raise RuntimeError(
+            "ADMIN_TOKEN must be set to a non-default value before Core starts"
+        )
+    return tok
@@
-def _admin_authed(request: web.Request) -> bool:
-    token = request.headers.get("X-Admin-Token") or request.query.get("admin_token")
-    return token == admin_token()
+def _admin_authed(request: web.Request) -> bool:
+    if request.method in ("POST", "PATCH", "PUT", "DELETE"):
+        origin = request.headers.get("Origin")
+        if origin and origin not in ALLOWED_ADMIN_ORIGINS:
+            return False  # cross-origin write, refuse even with token
+    token = request.headers.get("X-Admin-Token")  # header only, no query string
+    return bool(token) and hmac.compare_digest(token, admin_token())
@@ async def handle_admin_manifest(request):
-    if not isinstance(parsed, dict) or "nodes" not in parsed:
-        return web.json_response({"error": "manifest_missing_nodes"}, status=400)
+    errors, _warnings = validate_manifest(
+        parsed, state.manifest_path.parent, origin="remote_write",
+    )
+    if errors:
+        return web.json_response(
+            {"error": "manifest_invalid", "errors": errors}, status=400,
+        )
```

```diff
--- a/core/supervisor.py
+++ b/core/supervisor.py
@@ def make_script_resolver(...):
-    cmd = node.get("metadata", {}).get("runner", {}).get("cmd")
-    if cmd:
-        return ["/bin/sh", "-c", cmd]
+    cmd = node.get("metadata", {}).get("runner", {}).get("cmd")
+    if cmd:
+        if os.environ.get("MESH_ALLOW_INLINE_RUNNERS") != "1":
+            raise ValueError(
+                f"node {node['id']!r} declares metadata.runner.cmd but "
+                "MESH_ALLOW_INLINE_RUNNERS is not set"
+            )
+        return ["/bin/sh", "-c", cmd]
```

Plus `[opinionated]` `dashboard/vite.config.ts`: refuse to start if `ADMIN_TOKEN` is
unset or default; never inject the token unless the request originates from the dev
server's own origin.

The validator gets two new rules (`secret_inline_on_remote_write`,
`runner_cmd_on_remote_write`) so the same checks are exercised in unit tests, not just
at runtime.

### Fix 2 — Replace public `_derive` with per-machine master secret (V-08)

**Layer: `[protocol]`** for `_resolve_secret`'s failure mode. **`[opinionated]`** for
the specific master file path and shell script.

```diff
--- a/core/core.py
+++ b/core/core.py
@@
-    def _resolve_secret(self, node_id: str, spec: str) -> str:
-        if spec.startswith("env:"):
-            var = spec[4:]
-            val = os.environ.get(var)
-            if val:
-                return val
-            val = hashlib.sha256(f"mesh:{node_id}:autogen".encode()).hexdigest()
-            os.environ[var] = val
-            return val
-        return spec or hashlib.sha256(f"mesh:{node_id}:autogen".encode()).hexdigest()
+    def _resolve_secret(self, node_id: str, spec: str) -> str:
+        if spec.startswith("env:"):
+            var = spec[4:]
+            val = os.environ.get(var)
+            if not val:
+                raise RuntimeError(
+                    f"node {node_id!r} declares identity_secret env:{var} "
+                    "but the variable is unset"
+                )
+            return val
+        if not spec:
+            raise RuntimeError(
+                f"node {node_id!r} has no identity_secret; refusing to autogenerate"
+            )
+        return spec
```

```diff
--- a/scripts/_env.sh
+++ b/scripts/_env.sh
-_derive() { printf "mesh:%s:dev" "$1" | shasum -a 256 | cut -d' ' -f1; }
+_master_path="${HOME}/.config/raven_mesh/secret_master"
+if [ ! -f "$_master_path" ]; then
+    mkdir -p "$(dirname "$_master_path")"
+    head -c 32 /dev/urandom | xxd -p -c 64 > "$_master_path"
+    chmod 600 "$_master_path"
+fi
+_derive() {
+    printf "%s:%s" "$(cat "$_master_path")" "$1" \
+        | shasum -a 256 | cut -d' ' -f1
+}
```

`_derive` is `[opinionated]` (it's the dev-loop convenience shipped by *this* product);
the protocol-side change is that Core now refuses to fabricate a secret behind the
operator's back. Same-host attackers reading source no longer learn the secret.

### Fix 3 — Bound per-node delivery queues (V-06)

**Layer: `[protocol]`.** Generic safety property: an unbounded queue is a
language-agnostic memory leak.

```diff
--- a/core/core.py
+++ b/core/core.py
@@ async def handle_register(request):
-    queue: asyncio.Queue = asyncio.Queue()
+    queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
@@ async def _route_invocation(...):
-        target_conn["queue"].put_nowait({"type": "envelope", "data": env})
+        try:
+            target_conn["queue"].put_nowait({"type": "envelope", "data": env})
+        except asyncio.QueueFull:
+            await state.audit(
+                type="invocation", from_node=from_node, to_surface=to,
+                decision="denied_queue_full", correlation_id=correlation_id, details={},
+            )
+            state.emit_envelope(env=env, direction="in", signature_valid=True,
+                                route_status="denied_queue_full")
+            return 503, {"error": "queue_full", "node": target_node}
```

The audit log already feeds the dashboard tap, so back-pressure becomes immediately
observable. The opinionated layer is unaffected — every node keeps the same SDK.

## 8. What the protocol cannot fix

By design, several issues live in `nodes/*` and must not be fixed in `core/`:
- **V-07** is a Claude-CLI flag the operator chose. Gating it on `MESH_ALLOW_DANGEROUS`
  is the right shape, but the gate belongs in the runner, not in core.
- **V-09 / V-11** are policy choices about how `voice_actor` and `nexus_agent_isolated`
  handle their respective vendors' credentials.
- **V-17** is whether the kanban surface chooses to be reachable to other browser tabs.
  Other nodes might *want* loopback-no-auth (a dev-time scratch surface).
- **V-20** is a per-surface schema problem; the validator can enforce the regex but
  the regex itself is the surface owner's call.

If we move any of these into `core/`, the protocol stops being substitutable —
forking RAVEN_MESH and replacing every node would inherit policy that doesn't apply.

## 9. Open questions

1. **Validator strict-on-remote-write — separate flag, or always-on?** Recommendation:
   always-on for security rules (inline secrets, inline runner.cmd, schema-path escape);
   opt-in for hygiene rules (dead edges, missing env vars). Two thresholds, not one
   global switch.
2. **Replay window — strict 60s, or per-deployment?** Some nodes (cron, batch ingestion)
   may legitimately produce envelopes minutes apart from clock drift. Recommendation:
   protocol mandates a window check; the bound is configurable per Core (`MESH_REPLAY_WINDOW_SEC`,
   default 60).
3. **Ephemeral OpenAI tokens — voice_actor only, or part of an SDK contract?** If the
   pattern repeats across `nodes/*`, factor it into `node_sdk/` as `request_ephemeral`,
   keeping the long-lived key in a dedicated process. That's a `[protocol]` SDK helper,
   but only worth it once a second node needs the same shape.

## 10. What to ship next

1. The 3 fixes above as one PR — all `[protocol]` changes plus the dashboard/Vite
   `[opinionated]` companion. Adds ~80 LoC, removes ~10. Closes 4 of the 6 critical/high
   findings outright and downgrades the rest.
2. Validator wiring per the design note's Stage 1, *plus* the three security rules
   noted in §5. The wiring is `[protocol]`; the test that flags `full_demo.yaml`'s
   undeclared `nexus_agent` is `[opinionated]` (it's about *our* manifest).
3. Defer V-04, V-05, V-12, V-16 to a second pass; defer V-07/V-09/V-10/V-11/V-17/V-19/
   V-20 to per-node owners. Track the gap matrix in §6 as it drains.

The single property to keep checking after each step: **a fork that throws away every
node and the dashboard should still feel correct on the changes that landed in
`core/`.** Anything that fails that test has leaked opinion into the protocol.
