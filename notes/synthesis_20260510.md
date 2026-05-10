# RAVEN_MESH — synthesis, morning of 2026-05-10

Reviewer's note: I read every commit since the v0.4 ship, the protocol spec, the core, the SDK, the four nodes that landed today (kanban, nexus_agent, nexus_agent_isolated, voice_actor), the full_demo and voice_actor_demo manifests, and the relevant slice of yesterday's session transcripts (the WebRTC/WebSocket tradeoff, the live-edit honest read, and your "go for the supervisor in python" decision). I did not run the mesh; everything below is from reading the artifacts.

---

## 1. What we shipped today

A lot. Going commit-by-commit so you can see the cost:

| commit | what it solved | what it cost |
| --- | --- | --- |
| `dc100fe` v0.4 reference impl | Single-process Python Core (~430 lines, actually 678 now), node_sdk, four reference nodes, four dummies, 19 tests covering all PRD §7 flows including a no-SDK external node. The protocol-as-conformance-test discipline is intact. | None I can see. The base is clean. |
| `e1e6045` kanban_node | A second non-trivial capability node beyond webui. Mirrors webui's "browser mutations and mesh tool calls share the same mutator + SSE" pattern. 12 new tests. Good evidence the SDK + capability pattern reproduces. | Adds another disk-local data file (`data/board.json`) that doesn't survive a host move — same caveat as cron_node. |
| `b6ecbe6` admin endpoints + Mesh Dashboard + ui_visibility | Five admin endpoints (`/v0/admin/state`, `/admin/stream`, `/admin/manifest`, `/admin/reload`, `/admin/invoke`, `/admin/node_status`, `/admin/ui_state`), token-gated, plus a Vite/React/Tailwind dashboard at :5180 with Live Logs, Mesh Builder, Surface Inspector, UI Visibility toggles. ui_visibility helper shared across web-bearing nodes. | The dashboard now lives outside the protocol. It speaks `/v0/admin/*`, which is RAVEN-Mesh-specific; PROTOCOL.md doesn't document the admin surface. See §3. |
| `bcc1fe3` run_mesh.sh | Generic `run_mesh.sh <manifest>` script that parses node IDs and execs matching `run_<node>.sh` per node, prints UI links. | Cements the "you must hand-write a `run_<node>.sh` per node" contract. The mesh's process model is now a bash convention, which is precisely the gap your supervisor decision is meant to close. |
| `92b5ca4` human_node form auto-gen | `/schemas` endpoint on human_node proxies admin/state filtered to allowed targets; frontend renders forms from JSON Schema with smart fallbacks. Big UX win — you can now drive any allowed surface from the human dashboard without hand-writing JSON. | Human_node now talks to `/v0/admin/*` directly. Two nodes (dashboard + human_node) now depend on the admin token. Centralization of the admin secret is becoming a thing — see §4. |
| `2a82e36` _env.sh: NEXUS_AGENT_SECRET export | Bug fix: nexus_agent and core derived different secrets ⇒ 401 on register. | Two-worker integration bug. Real cost: this is the second secret-sync incident I can see (VOICE_ACTOR_SECRET added in the same file alongside the older VOICE_SECRET — see §4). |
| `a5b8e21` nexus_agent | The big one. Actor-kind node that spawns `claude` per inbox message with an MCP stdio bridge wired to a loopback control HTTP server. Inspector UI on :8804. Identity ledger pattern (`identity.md` + `memory.md` + `skills/`). 8 new tests. | Each inbox message spawns a fresh `claude` process. No batching, no debouncing. Default model `claude-sonnet-4-6`. See §3 (cost & latency). |
| `a4408b9` tool isolation | `--strict-mcp-config` + `--tools ""` to stop the spawned `claude` from inheriting your global MCP servers (imessage, playwright, ios-simulator, etc.) and built-ins. Verified the agent now sees exactly 7 mesh tools. | None. This is just correct. The fact it took a fix means the v0 spawn was leaking your full host capabilities into the mesh agent — would have been a real footgun once anyone else ran it. |
| `9df078c` nexus_agent_isolated | Sibling of nexus_agent that runs `claude` inside a Docker container. Auth via macOS keychain extraction (`security find-generic-password -s "Claude Code-credentials" -w`) → `CLAUDE_CODE_OAUTH_TOKEN`. `host.docker.internal` networking, named ledger volume, baked-in bridge. | Per-message `docker run` is slow (cold-start ~1-2s on top of the LLM round-trip). Two near-duplicate node implementations (host + isolated) are diverging — see §3. |
| `3b19c70` voice_actor + `8de8415` polish + `b1ddbf4` .env loader | OpenAI gpt-realtime-2 over raw aiohttp WebSocket. sounddevice 24kHz PCM16. 5 surfaces (start_session, stop_session, say, session_status, ui_visibility). Mesh-tool injection: at session start, voice_actor introspects the mesh and registers one Realtime function-call tool per outgoing edge so the model can hand off to other nodes. | 800-line `voice_actor.py` is the largest file in `nodes/`. Audio I/O + Realtime client + mesh-tool dispatch + web inspector all live in the same file. Manageable now, but it's the canary for "node files are getting too big." |

By the numbers: 67 tests collected (up from 19 at the v0.4 cut), four real production-shaped nodes added (kanban, nexus_agent, nexus_agent_isolated, voice_actor), a working dashboard, a generic mesh runner, and a working voice-to-agent loop. In one day.

---

## 2. Patterns I noticed

**The "capability + inspector" pattern is now load-bearing.** Every node you've shipped today follows the same shape: a `MeshNode` subclass for protocol concerns, an aiohttp app for an inspector UI on a dedicated port (8801–8807), an SSE `/events` channel for browser live-update, a `ui_visibility` surface, and a JSON state endpoint. This works really well — kanban_node and webui_node feel like the same node with different mutators. But the pattern is reimplemented per-node. There's no `node_sdk.WebInspector` helper. That's the next obvious extraction.

**The MCP bridge is the agent's universal driver.** Both nexus_agent and nexus_agent_isolated expose the same five tools to claude (`mesh_invoke`, `mesh_list_surfaces`, `mesh_send_to_inbox`, `memory_read/write`, `list_skills/read_skill`). The bridge is a stdio MCP server that calls back into a loopback HTTP control server gated by an `X-Control-Token`. This is structurally the right abstraction: the agent doesn't know it's inside the mesh; it just sees a small tool set. The same pattern would work for any "agent harness" node — Codex CLI, Aider, custom Python agents — with no protocol changes.

**Voice_actor's mesh-tool injection is the brightest idea in today's diff.** `_build_mesh_tools` introspects the mesh at session start and registers one Realtime function-call tool per outgoing edge, with auto-generated descriptions that distinguish inbox handoffs from request/response tools and explicitly skip `ui_visibility` as noise. This is the first node that makes the manifest *visible to a model at runtime in a way the model can act on*. The pattern ports straight to nexus_agent (it already does this through MCP, less elegantly) and to any future node that wants to expose mesh capabilities to a third party. This is the "manifest as live API" idea, working.

**Identity = `identity.md` + `memory.md` + `skills/` is real now.** Both nexus agents load identity into the system prompt, mount a mutable memory.md that the agent can read/write through a tool, and discover skills on demand. This mirrors the NEXUS pattern. It's also exactly what will become a "ledger node" later — the file-on-disk model is fine for now but the moment you have two harnesses sharing memory you want a `memory_node` capability surface.

**Hidden complexity:** The "inspector" web servers each have their own SSE implementation. So does Core (`/v0/stream`), so does Core's admin (`/v0/admin/stream`), so does the dashboard's React `EventSource`/`fetch+ReadableStream` bridge to inject the admin token. Every one is correct in isolation; together it's a lot of bespoke streaming code with subtly different reconnect/heartbeat policies. This is the kind of thing OTP gives you for free in Elixir, but in Python it's all hand-rolled. Worth noting before you write a sixth one.

**Hidden complexity #2: secret sprawl.** `_env.sh` defines 14 env vars, including two that point at the same secret (`VOICE_SECRET` and `VOICE_ACTOR_SECRET`, both derived from `voice_actor`). Core, dashboard, and human_node all need `ADMIN_TOKEN`. The control servers use a separate per-process `X-Control-Token`. The keychain has the OAuth token. Auth is fine but the ceremony of getting all this lined up is now its own onboarding cost.

---

## 3. Tensions and contradictions

**Your stated direction vs. the code:** On May 9 4:56 PM you said *"the shape of the protocol matters right now, implementation doesn't."* The code is honoring that. But two things since then have started bending in the other direction:

1. **The dashboard speaks `/v0/admin/*`, which is *not* in PROTOCOL.md.** PROTOCOL.md §1–§8 documents the seven canonical endpoints (`register`, `invoke`, `respond`, `stream`, `healthz`, `introspect`). The seven `/v0/admin/*` endpoints (`state`, `stream`, `manifest`, `reload`, `invoke`, `node_status`, `ui_state`) are documented only inside `core.py`'s docstring. If "the protocol is the contract," then the dashboard is currently coupled to a Core-implementation extension that won't necessarily exist on the BEAM rewrite. **Decision needed:** are admin endpoints part of the protocol or part of the prototype? The PROTOTYPE.md "how to refactor away" guidance assumes the latter, but the dashboard's existence assumes the former.

2. **`full_demo.yaml` declares 10 edges from `nexus_agent` but does not declare nexus_agent as a node** (`grep -c "id: nexus_agent" full_demo.yaml` → 0; `grep -c "from: nexus_agent"` → 10). `core.load_manifest` (`core/core.py:117-118`) adds these edges to the set without validating that source or target nodes exist. The edges are dead but the manifest is allowed. This is exactly the kind of silent inconsistency the dashboard's "Mesh Builder" should be flagging. It's also what will bite you if you ever do strict edge validation later — production code will already depend on the lenient semantics. **Recommend:** fix the manifest, then make Core reject edges that name an undeclared `from` or `to` node, before the lenient behavior calcifies.

**`replace_active=True` on voice_actor (`voice_actor.py:504, 583-594`) silently kills any in-flight Realtime session if a new `start_session` arrives.** That's defensible for a voice surface. But the same pattern is *not* used in nexus_agent: `handle_inbox` serializes runs behind a single `asyncio.Lock` (`agent.py:118`). So inbox messages queue up; voice sessions stomp. Two different concurrency models for two actors that an operator might reasonably expect to behave the same way. Pick one and document the contract — "actors serialize" or "actors replace" — in the SDK or PROTOCOL.md.

**Inbox is `fire_and_forget` per manifest, but the handler returns a payload anyway (`agent.py:172-177`).** The comment says node_sdk "drops it when the surface is FAF." It does (`node_sdk/__init__.py:283`), but this means the handler is doing work — building tokens, status, session_id — that's never delivered. If you ever change the surface to request_response without changing the manifest, the response goes out unexpectedly. Tiny risk; worth a one-line comment that the return value is dead-code-by-design.

**nexus_agent vs nexus_agent_isolated divergence.** Both have ~470-line `agent.py` files that implement essentially the same logic, plus a near-identical `cli_runner.py` / `docker_runner.py` pair, plus a near-identical `mcp_bridge.py`. Today the divergence is small. In a week, when one gets a feature the other doesn't, you'll have two nodes that *almost* behave the same. **Recommend:** factor `agent.py` into a shared `nodes/agent_base.py` and keep only the runner difference in each subclass. Or — better — make the runner pluggable behind a `--runtime host|docker` flag on a single nexus_agent.

**The supervisor decision contradicts the v0.4 README.** The README still says *"The BEAM/Elixir refactor is a future state, not this build,"* implying we won't build OTP-shaped things in Python. Your 12:36 AM message overrode that ("real systems on the python version to thoroughly understand what we need for v1"). Both are defensible. But the README needs an update — without one, the next contributor (or the next you, in three weeks) will hit the supervisor work and ask "wait, didn't we say we wouldn't?"

---

## 4. What's missing / underspecified

**Security:**

- **`ADMIN_TOKEN` defaults to `"admin-dev-token"` (`core/core.py:39`) and the `/v0/admin/*` endpoints, including `/admin/manifest` (rewrites the manifest from POST body) and `/admin/invoke` (synthesizes a signed envelope from any registered node), are gated only by this token.** On `127.0.0.1` that's fine. The moment you put Core behind Tailscale, the default is a remote-write hole. Add: refuse to start with the default token if `MESH_HOST != 127.0.0.1`, or always log a loud warning, or rotate to a random token on first boot and write it to `.admin_token` 0600.
- **`nexus_agent.cli_runner` runs `claude` with `--dangerously-skip-permissions` (`cli_runner.py:116`).** Combined with `--tools ""` and `--strict-mcp-config` this is well-contained: the only side effects the agent can have are mesh tool calls. But the flag's name should make us nervous — if anyone ever loosens `--tools`, the safety vanishes. Add a comment + a test that asserts the args list always contains `--tools ""` (currently no such guard).
- **Keychain extraction in `nexus_agent_isolated/docker_runner.py:50-65` shells out to `security find-generic-password` with `subprocess.run`. The container env then logs `CLAUDE_CODE_OAUTH_TOKEN=...`** — `cli_spawn` event redacts it (`docker_runner.py:154-161`), good, but `docker run` itself puts the env var on the docker daemon's audit log on macOS. Acceptable for personal use; document it.
- **No rate limit anywhere.** `human_node` can pump unlimited messages into `nexus_agent.inbox`; there's nothing stopping an inbox from spiraling claude spawns. Cheap fix: a per-source-node rate limit in Core's edge check. Real fix: that's what an approval node is for, and no one wired one in front of nexus_agent.

**Testability:**

- **No end-to-end test that `voice_actor` actually injects mesh tools at session start.** The voice_actor tests (`tests/test_voice_actor.py`) cover registration, schemas, and graceful-degradation envelopes, but not `_build_mesh_tools`. That function is the most novel idea in the diff and the one most likely to break silently when the introspect format changes.
- **No test for the dashboard `/v0/admin/manifest` round-trip.** `test_admin.py::test_admin_manifest_writes_and_reloads` is implied by the commit message but I want to verify rollback works on a malformed YAML payload after a successful prior write — that's the one edge case where data loss is possible.
- **No load test of the SSE delivery queues.** `core.py:198` creates an unbounded `asyncio.Queue` per connected node. A slow consumer + a fast producer = unbounded memory. Set `maxsize=1024` like the admin streams already do (`core.py:469`).

**Protocol gaps:**

- **No `Last-Event-ID` resume.** PROTOCOL.md §3.2 says it's "reserved for v0.x." Today, every reconnect re-registers and loses any deliveries that were in-flight on the old SSE connection. With voice (low tolerance for missed events) and nexus_agent (long-running runs), this will start mattering. Even a cap-bounded ring buffer of last-N envelopes per node, replayed on reconnect, would close most of the window.
- **No node liveness signal beyond SSE connection state.** `/v0/admin/state` shows `connected: true|false`, but a node can be SSE-connected and completely wedged. There's no health-ping. Add a periodic `event: ping` from Core that nodes ack, with a configurable timeout that flips `connected` to `degraded`.
- **No idempotency keys on `invoke`.** Replays across a reconnect would double-fire side effects. Probably worth a one-paragraph note in PROTOCOL.md that `id` is the natural idempotency key and recipients should treat duplicate `id`s as a no-op response. Today nothing in the SDK enforces or even hints at this.

**Architecture gaps that will hurt:**

- **Core has zero awareness of node *processes*.** It knows about declared nodes (manifest), connected nodes (sessions), and edges. Process lifecycle is an external concern — `run_mesh.sh` parses the manifest and execs `run_<node>.sh`. This is exactly the surface area the supervisor work is supposed to fill. Worth being explicit: when the supervisor lands, the manifest needs a `runtime` block that's actionable (not just `local-process` as an opaque label like today, see PROTOCOL.md §6).
- **The "external-language node" example in `tests/test_protocol.py::test_step_10_external_language_node` is the most under-leveraged asset in the repo.** It proves the protocol is genuinely portable. But there's no `examples/` directory, no Go/Rust/TS port, no README pointer that says "here's how to write a node in 30 lines without our SDK." If "the shape of the protocol matters" is the thesis, that example deserves to be a top-level demo, not a buried test case.

---

## 5. Sharp questions for Colton

These are the questions where I think the answer changes a lot of downstream decisions. I've ordered them roughly by leverage.

1. **Are `/v0/admin/*` endpoints part of the protocol or part of the prototype?** If part of the protocol, document them in PROTOCOL.md and commit to the BEAM Core implementing them too. If part of the prototype, the dashboard needs to be structured as a *node* that talks to Core via the documented protocol, not via privileged admin endpoints. Right now we're doing both implicitly, which means the BEAM rewrite gets two very different deliverable specs.
2. **What's the supervisor's contract with the manifest?** When the YAML changes a node's `runtime` from `local-process` to `docker:img`, does the supervisor restart the node? When edges change, does it just reload? When a new node is added, does it spawn? Answering this defines what `/v0/admin/manifest` *should* return — today it returns "ok, reloaded" with no diff. The supervisor needs a structured diff in the response or it can't act. (Side benefit: the dashboard's "save manifest" button gets a much better UX.)
3. **Is the agent expected to be one process or many?** Right now, every inbox message to nexus_agent spawns a fresh `claude` (host) or `docker run` (isolated). That's expensive — Docker is ~1-2s cold start on top of LLM latency. For a "voice → agent → tool" loop where you want sub-second response, that's already the bottleneck. Do we want a long-running `claude` daemon mode, batch consecutive messages within N seconds, or accept the cost?
4. **How do you want secrets to be managed once we're cross-host?** `_env.sh` derives every node secret from a literal string. This is fine for one machine. The moment Core lives on the Mac mini and a node lives on the MacBook, the MacBook needs to know all those secrets. Plaintext over Tailscale isn't *terrible*, but the keychain extraction pattern in `nexus_agent_isolated` proves we have a real pattern for "node fetches its secret from a local secret store at boot." Do we want to formalize that (a `secret://` scheme in the manifest) or stay with env vars for v1?
5. **Should `human_node` be the only "operator" node?** Today it's both the operator UI *and* an actor with broad outgoing edges to most surfaces. The dashboard is also an operator surface that bypasses the relationship graph entirely (via `/admin/invoke`). If you took human_node away tomorrow, the dashboard would still work. So which one is "the" operator interface? Answering this reduces a lot of UI duplication.
6. **Should Core enforce manifest sanity at load time?** Specifically: edges referencing undeclared nodes (the `full_demo.yaml` nexus_agent issue), schemas that don't parse, surfaces with unrecognized `type` or `invocation_mode`, duplicate node IDs. Today the loader is permissive. Strict mode catches real bugs but breaks the lenient migration story. I'd push for strict mode + a `--unsafe-manifest` opt-out, but it's your call.
7. **Where does memory live in v1?** `nodes/nexus_agent/ledger/memory.md` and `nodes/nexus_agent_isolated/ledger/memory.md` are different files. If both agents ever want to share a fact, they can't. Is memory per-agent (today's model), per-mesh (a `memory_node` capability), or per-user-identity? The answer changes whether `memory.md` belongs in the node directory or in a separate node entirely.
8. **What does "node identity" mean in a multi-host world?** Today, `nexus_agent` is one node-id. If we run two nexus_agent processes on two hosts, they collide on registration (Core kicks the older one — `core.py:189-195`). Do we want N replicas of a single logical node, distinct node IDs per replica, or a sharding key in the registration body?
9. **Do you actually want multiple CLI agents long-term, or is this Claude-only?** The MCP bridge pattern would port to Codex CLI, Aider, or any tool that supports MCP. But the auth (`security find-generic-password -s "Claude Code-credentials"`), the model defaults (`claude-sonnet-4-6`), and the `--dangerously-skip-permissions` flag are all Claude-CLI-shaped. If we ever want a Codex agent, half of `cli_runner.py` needs to become a strategy pattern. Worth knowing now.
10. **Is voice_actor's "introspect → register tools per edge" pattern the canonical way for a model to discover the mesh, or a one-off?** If canonical, it should move into a SDK helper (`MeshNode.discover_tools_for_model("openai-realtime"|"openai-function"|"anthropic-tool")`). If a one-off, voice_actor stays as the only node that does this and we accept the duplication when the next model harness shows up.

---

## 6. One bold proposal

**Make the dashboard a real mesh node.**

Today the dashboard is a Vite/React app that talks directly to `/v0/admin/*` with the admin token. That's expedient, but it bifurcates the protocol: there's the documented protocol (HMAC envelopes, edge-gated, schema-validated, audited) and the admin protocol (token-gated, untyped POST bodies, no edge check, only partially audited). Two security models. Two ways to drive the mesh. Two implementations to keep in sync as the BEAM rewrite happens.

**Proposal:** the dashboard registers as a node — `dashboard_node`, kind `actor` — with a tightly scoped manifest entry. Its outgoing edges are exactly the surfaces the operator is allowed to drive (`webui_node.show_message`, `kanban_node.create_card`, etc.). It gets its envelopes signed by an HMAC secret like every other node. The "Try It" panel becomes a normal `mesh.invoke`. The Live Logs panel subscribes to a *new* surface — `core.audit_stream`, an inbox surface that Core declares on itself — instead of a privileged admin endpoint. The Mesh Builder writes to a *new* surface — `core.set_manifest` — that's edge-gated like everything else.

What this buys:

1. **One protocol, one auth model.** Everything is HMAC + edges + schemas. The admin token goes away (or shrinks to a single `core.set_manifest` permission), eliminating the "default-admin-token over Tailscale" foot-gun (§4).
2. **The dashboard becomes a working example of a non-Python node.** Right now no node speaks the protocol from JS. The dashboard's `lib/mesh.ts` (which doesn't exist yet) becomes the canonical TypeScript mesh client and proves the protocol is what the README says it is.
3. **The BEAM rewrite spec gets simpler.** "Implement the seven `/v0/*` endpoints; nothing else." The admin endpoints don't need to be ported, because they don't exist anymore.
4. **Audit covers the operator's actions automatically.** Today, when you click "change webui color to teal" in the dashboard, the audit log records `from_node: dashboard_synthetic` (or whatever `/admin/invoke` synthesizes). With this change, it records `from: dashboard_node, to: webui_node.change_color`, and you can grep your own actions out of `audit.log` like any other node's.
5. **The supervisor work gets a clean API.** Instead of inventing a new `/v0/admin/spawn_node` endpoint, Core declares a `core.lifecycle` surface with `start_node`, `stop_node`, `restart_node` tool surfaces. The supervisor is just a thing Core does in response to those mesh invocations. Same auth model, same audit, same dashboard wiring.

**Cost:** ~1 day of refactor (move the React app to consume an SSE-subscribed `core.audit_stream` surface, add HMAC signing to the JS client, declare `dashboard_node` and a `core_node` with the new surfaces in the demo manifest), plus a real spec decision about whether Core is "a node that also brokers" or "a broker that also has a few self-surfaces." I'd argue for the latter, declared in PROTOCOL.md as a single new section: "Core's self-surfaces."

This is the move that makes "the shape of the protocol matters, implementation doesn't" actually true. Right now, in implementation, two protocols live side by side and the dashboard depends on the wrong one.

---

## Closing

You shipped a working multi-node, multi-language, voice-driven, sandboxed-agent mesh in one day, on top of a 678-line single-file Python core. That's real. The supervisor work tonight is the right next step *for understanding* — even if the code is throwaway, the API surface you discover (what does `core.set_manifest` need to return? what fields does a `runtime` block need?) ports straight to OTP. Don't let "throwaway" stop you from being precise about what you're learning.

The single highest-leverage thing you could do this week, in my reading, is the dashboard-as-node refactor in §6. It collapses two protocols into one, deletes the admin-token foot-gun before it migrates onto Tailscale, and gives you the cleanest possible spec to hand to the BEAM rewrite. Everything else — supervisor, isolation polish, voice/agent loop tightening — builds on top of that decision.

— synthesis worker, 2026-05-10
