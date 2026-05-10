# Mesh-only ideas — 2026-05-10

Brainstorm of capabilities that can ONLY be built on RAVEN_MESH (or are dramatically better there). Each idea is graded on: which mesh primitive it leverages that's hard elsewhere, what's needed, effort, and why it's interesting.

Mesh primitives in scope:
- HMAC-signed envelopes (provable provenance, tamper-evident chains)
- Per-surface JSON Schemas (typed, validated tool surfaces; cross-language)
- Manifest = system architecture (declared edges → static auth graph)
- SSE deliver / single-broker routing (ordered, observable, replayable)
- `wrapped` envelope field (approval forwarding — used as primitive for chains)
- `correlation_id` + audit log (trace any chain across nodes/runtimes)
- `/v0/admin/invoke` + `/v0/admin/stream` (synthesize+observe envelopes from any registered node)
- Heterogeneous nodes (humans, AI agents, tools, data stores) addressed identically

---

## 1. Provenance Replay
**Tagline:** Replay any past envelope from `audit.log` against a node fork to see "what would have happened if X."

- **Mesh-native because:** the audit log is a complete, ordered, signed-record of every invocation that ever crossed Core. No app-level logging gives you the same: signatures + correlation chains + payloads in one stream.
- **Build:** `replay_node` capability that consumes `audit.log`, exposes `replay.start({correlation_id, target_fork})`. Uses `/v0/admin/invoke` to re-fire the original envelopes from synthetic forks.
- **Effort:** small (most plumbing exists; just stream + admin/invoke).
- **Why interesting:** time-travel debugging that no per-app system gives you for free.

## 2. Provenance Stamps
**Tagline:** Every output includes a `chain` of envelope IDs that produced it; verifier-node validates the chain against `audit.log`.

- **Mesh-native because:** `wrapped` already nests one envelope inside another, and every envelope is signed. Outside the mesh, you'd need bespoke OpenTelemetry plumbing per service to even get correlation IDs, never mind cryptographic provenance.
- **Build:** small `provenance_node` that walks `wrapped` chains and audit log to emit a "this answer came from {voice_actor → approval → claude → kanban_node}" certificate.
- **Effort:** small.
- **Why interesting:** for AI outputs especially — "why did the agent say this" is a first-class queryable answer.

## 3. Multi-Agent Debate + Judge
**Tagline:** Claude_node and gpt_node argue both sides of a question, judge_node arbitrates.

- **Mesh-native because:** Claude Code and OpenAI Realtime live as peer nodes already. Each is independently addressable, signed, and audited; the judge can verify both came from the right secret. Without the mesh you'd hand-roll routing + you'd lose the audit trail of who said what.
- **Build:** two AI runtimes already exist (nexus_agent uses Claude). Add `gpt_realtime_node` (or stub it w/ another claude instance under a different identity), `debate_orchestrator` (actor), `judge_node` (capability that reads both responses + emits verdict).
- **Effort:** medium (real LLM nodes), tiny (stubbed echo nodes with deterministic "opinions").
- **Why interesting:** demos cross-runtime composition; the audit log tells the whole story.

## 4. Mesh-aware Self-Composing Agent
**Tagline:** An agent that calls `/v0/introspect`, reads available surfaces + schemas, and composes a workflow on the fly without a programmer specifying which tools to call.

- **Mesh-native because:** every surface declares its schema in machine-readable form via the manifest. An agent can browse the mesh as a typed tool registry and plan against it without bespoke MCP definitions per node.
- **Build:** `meta_agent` actor — does `GET /v0/introspect`, feeds the schema + edges to an LLM, executes the plan. Reuse nexus_agent + give it an introspect tool.
- **Effort:** medium.
- **Why interesting:** you can add a new node with a manifest entry and the agent immediately knows how to use it.

## 5. Federated Mesh (Two Hosts, One Protocol)
**Tagline:** Two RAVEN_MESH instances on different machines connect via a `federation_node` on each side. Envelopes signed at A land verified at B.

- **Mesh-native because:** the wire protocol is already minimal + routing is by surface ID, not host. The federation node just bridges deliver events and respects signatures end-to-end. No transport address ever appears in an envelope.
- **Build:** `federation_node` capability with one tool `forward({remote_surface, payload})`. Configure mirrored manifests on both sides w/ shared secrets per pair.
- **Effort:** medium.
- **Why interesting:** distributed trust without re-inventing identity; the manifest is the federation policy.

## 6. Kanban-as-Workflow-Engine
**Tagline:** Each kanban column is a node; moving a card emits an envelope to the destination column's node, which runs a handler.

- **Mesh-native because:** column-as-node + signed transitions + audit = a Petri-net workflow engine where every state transition is provably authorized by the moving party. Manifest declares which transitions are legal as edges.
- **Build:** turn columns into capabilities; `move_card` invokes `column_<dest>.accept({card})`. Audit gives you full state history.
- **Effort:** medium.
- **Why interesting:** business workflows visible in plain manifest YAML; moves are receipted.

## 7. Cron-Triggered Multi-Agent Daily Standup
**Tagline:** Cron fires at 9am → fan-out invocation to each agent node → agents reply with status → aggregator → posts a digest envelope to human inbox.

- **Mesh-native because:** cron_node + multiple AI agent nodes + a uniform inbox surface = a pure mesh-topology DAG. The audit log = the standup transcript. Adding an agent = manifest line.
- **Build:** orchestrator capability `daily_standup.run`, scatter/gather over agent nodes, collate to human_node.inbox.
- **Effort:** small.
- **Why interesting:** daily-use; demonstrates fanout + composition.

## 8. Voice-Driven Mesh Inspector
**Tagline:** Speak "why did kanban fail at 3pm" → voice_actor → nexus_agent → audit-log query → spoken answer through voice_say.

- **Mesh-native because:** voice_actor + nexus_agent + audit log + voice_say are all peer nodes already. The chain itself appears in the audit log, so the inspector can introspect its own queries.
- **Build:** `audit_query_node` capability with `query({since, surface, decision})`; route voice_actor → nexus_agent → audit_query_node → voice_say.
- **Effort:** small (assuming voice_actor exists — it does).
- **Why interesting:** live debugging of a multi-agent system using only its own primitives.

## 9. Capability Marketplace via Mesh Introspection
**Tagline:** A `marketplace_node` exposes "available capabilities you don't currently have an edge to," and a `request_access` tool that opens an approval flow to add the edge.

- **Mesh-native because:** introspect already returns the full graph; manifest hot-reload is a real admin endpoint. So adding an edge at runtime = posting a new manifest. No new core feature required.
- **Build:** `marketplace_node` capability + UI; uses `/v0/admin/manifest` to write+reload after approval.
- **Effort:** small (writing yaml is easy; doing it safely via approval is the cool part).
- **Why interesting:** dynamic auth graphs without writing per-app permission systems.

## 10. Cross-Agent Skill Sharing via Ledger Writes
**Tagline:** Agent A learns a skill, writes it directly into agent B's ledger via a mesh tool surface; B's next run picks it up.

- **Mesh-native because:** nexus_agent has a persistent ledger AND exposes ledger-write surfaces over the mesh. Edge-gated: only specific peers can teach. No file-system trust assumptions.
- **Build:** `ledger_write` surface on nexus_agent (largely exists per `test_control_memory_read_and_write`); a teacher agent that calls it.
- **Effort:** small (mostly exists; new edges + a small "teach" actor).
- **Why interesting:** AI agents collaborating on memory, with a signed receipt of who taught what.

## 11. Mesh-as-Database (Append-only, Signed)
**Tagline:** `audit.log` is queried like a database via a `mesh_db.query` capability — joins on `from_node`, `correlation_id`, `decision`.

- **Mesh-native because:** audit.log already IS the system's full history of state transitions. A query node just reads it. No special instrumentation in any node ever required.
- **Build:** capability with one tool `query({where, group_by})`. Backed by a tiny in-memory loader of `audit.log`.
- **Effort:** tiny.
- **Why interesting:** observability with no extra integration; reusable from any peer.

## 12. Schema-Diff Compatibility Checker
**Tagline:** `compat_node` reads two manifests (current + proposed) and returns a list of breaking changes (edges removed, schemas tightened, surfaces deleted).

- **Mesh-native because:** the manifest IS the deployment artifact. Schema-diff is meaningful precisely because everything is declared.
- **Build:** capability `compat.check({current_yaml, proposed_yaml})` that loads both, walks edges + schemas, returns breakage list.
- **Effort:** tiny.
- **Why interesting:** safe-deploy story for a production mesh; the kind of thing usually needs a CI pipeline + bespoke tooling.

## 13. Time-Sliced Replay Diff
**Tagline:** Pick correlation_id X. Replay it against current mesh AND a forked mesh w/ a node swapped out → diff the response. "What if we used GPT instead of Claude here?"

- **Mesh-native because:** combines (1) Provenance Replay + (4) Self-Composing — same envelope, different node behind a surface. Possible because surfaces are addressable, not implementations.
- **Build:** wrap idea #1 with a forked Core boot using a substitute manifest. `diff_node` capability that aligns by step.
- **Effort:** medium.
- **Why interesting:** A/B testing entire workflows.

## 14. Authority-Bound Voice Commands
**Tagline:** Voice command → voice_actor → approval_node → target. The approval node only auto-approves commands signed within the last 30s by a paired hardware-token node (e.g. yubikey_node).

- **Mesh-native because:** approval flow + signed envelopes makes this a 4-line policy: "approve iff a recent envelope from yubikey_node carries a matching nonce." Outside the mesh you'd write custom auth glue per voice command.
- **Build:** `yubikey_node` (stubbed in v0 with a ttl token), `policy_approval` capability that checks envelope + nonce window, edges declared.
- **Effort:** small.
- **Why interesting:** real-world security primitive composed from off-the-shelf nodes.

## 15. Per-Envelope Cost Accounting
**Tagline:** Each LLM-runtime node, on response, emits a side-envelope to `accounting_node.record({correlation_id, tokens, $})`. `accounting_node` joins on correlation chains and prints "this user request cost $0.37 across 4 model calls."

- **Mesh-native because:** correlation_id is already universal. The accounting node doesn't have to be wired into each agent SDK — it just listens at a known surface, and you add an edge per agent.
- **Build:** `accounting_node` capability + a tiny middleware on AI nodes that fires-and-forgets a record envelope after each response.
- **Effort:** small.
- **Why interesting:** uniform cost observability across heterogeneous AI runtimes.

---

## Ranked by demo-impact-per-hour

| Rank | Idea | Effort | Demo punch |
| ---- | ---- | ------ | ---------- |
| 1 | **#11 Mesh-as-Database** | tiny | One node, audit-log queries, reusable from voice / kanban / anywhere — "the system documents itself" moment |
| 2 | **#2 Provenance Stamps** | small | Visible certificate stamp on every output; instantly explains the trust story |
| 3 | **#1 Provenance Replay** | small | "Replay yesterday's 9am cron and watch it run again" is a magic-trick demo |
| 4 | **#7 Daily Standup orchestrator** | small | Composes existing nodes into a real daily-use feature |
| 5 | **#12 Schema-Diff Compat** | tiny | Sells the manifest-as-spec story to any infra-minded viewer |
| 6 | **#15 Cost Accounting** | small | Pragmatic; lights up immediately on any LLM call |
| 7 | **#8 Voice Mesh Inspector** | small | Voice → audit query → voice. Highly demoable. |
| 8 | **#9 Marketplace + manifest hot-reload** | small | Live edges getting added — visceral |
| 9 | **#10 Skill Sharing via Ledger** | small | Less visual but conceptually deep |
| 10 | **#14 Authority-bound voice** | small | Niche but "yubikey via mesh" lands well |
| 11 | **#6 Kanban-as-Workflow** | medium | Bigger lift, broad payoff |
| 12 | **#3 Debate+Judge** | medium | Needs two real LLMs; rich demo |
| 13 | **#4 Self-Composing Agent** | medium | Powerful but easy to flop on screen |
| 14 | **#5 Federation** | medium | Setup-heavy for the punchline |
| 15 | **#13 Replay Diff** | medium | Builds on #1; ship #1 first |

---

## Pick: **#11 Mesh-as-Database** + a thin slice of **#2 Provenance Stamps**

Why: they're synergistic and tiny. A `mesh_db_node` is the foundation — it parses the running Core's `audit.log` and serves arbitrary queries via a typed mesh surface. Adding a thin `provenance.trace({correlation_id})` tool on top of it produces the full upstream chain of envelopes that led to a given response — that's the demo moment.

Together they:
- Need only the existing audit.log (no new core feature)
- Demonstrate three mesh primitives at once (audit log + surfaces + correlation chains)
- Run end-to-end via a voice_actor → mesh_db → printed result
- Test cleanly in-process against the existing core_server fixture
