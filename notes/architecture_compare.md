# RAVEN_MESH Architecture Comparison — Protocol-Layer Language Decision

**Author:** Synthesis pass over the four prototype ANALYSIS docs
**Date:** 2026-05-10
**Scope:** Protocol layer (`core/`, `node_sdk/`, supervision contract, admin API). The dashboard, kanban_node, voice_actor, webui, and any opinionated node are **out of scope** for this decision; they ride on whatever protocol implementation we pick.

> **Layer tag.** Every recommendation in this document targets the **protocol layer**. The protocol must support unknown future nodes — kanban + voice + dashboard + 100 use cases we haven't shipped — so the implementation language is judged on how cleanly it lets the protocol stay minimal, not on how nicely it lets us write any specific node.

---

## 1. Side-by-side matrix

Numbers come directly from the four ANALYSIS docs (`elixir_mesh/PORTING_ANALYSIS.md`, `rust_mesh/ANALYSIS.md`, `go_mesh/ANALYSIS.md`, `nats_pivot/PIVOT_ANALYSIS.md`). All "Python" entries use `core/core.py` + `node_sdk/__init__.py` (1,164 LOC) as the baseline.

| Axis | Python (incumbent) | Elixir / BEAM | Rust / tokio+axum | Go 1.22+ | NATS-as-transport |
|---|---|---|---|---|---|
| **Protocol LOC** (broker + SDK at parity) | 1,164 | **730** (~63%) | 1,361 (~117%) | 1,215 (~104%) | 511 (~44%) |
| **Tests passing** | 19 (`tests/test_protocol.py`) | 12 (`mix test`) | 6 (`cargo test`) | 4 packages clean, race-clean | functional demo + bench |
| **Build artifact** | interpreter + ~12 MB site-packages | ~1.6 MB BEAM tree + ERTS runtime | **5.9 MB single binary** (LTO + strip) | 6.7 MB single binary (stripped) | n/a (rides nats-server, ~12 MB) |
| **Clean release build time** | n/a | n/a (compiled per-module) | 26.4 s release; 58 s cold | seconds | n/a |
| **Cold start → 200 healthz** | ~150 ms (4-node manifest) | seconds (BEAM cold start) | **23 ms** | < 10 ms | inherits NATS (~ms) |
| **Idle RSS** | ~47 MB | 60–80 MB | **8.4 MB** | ~14.6 MB | nats-server ~10–15 MB + clients |
| **Native supervision primitive** | none (hand-rolled `supervisor.py`, 474 LOC) | **DynamicSupervisor + Registry** (5-line `start_node`) | none (337 LOC hand-rolled, restart sliding-window) | none (~180 LOC `one_for_one` reimpl) | none (orthogonal — must live above NATS) |
| **Native message-passing primitive** | `asyncio.Queue` per node | **GenServer mailbox = the node** (zero pending-future bookkeeping) | `tokio::sync::mpsc` + manual fanout (`Vec<Sender>` prune-on-`try_send`) | goroutine + `chan Envelope` (~30 LOC fanout, drops slow subs) | NATS subjects (`subscribe(subj, queue=…)` for fan-out free) |
| **Native pubsub / admin tap** | none (40 LOC SSE plumbing in `handle_admin_stream`) | **Phoenix.PubSub** (~5 LOC at call sites) | `async_stream::stream!` over moved receiver, `'static` lifetime constraint (~50 LOC) | `http.Flusher` + heartbeat ticker (~50 LOC) | any subscriber to `audit.>` is a tap |
| **HMAC + canonical JSON** | `json.dumps(sort_keys=True)` (~20 LOC) | `:crypto.mac/4` + iodata builder (82 LOC, byte-identical) | `hmac` + `sha2` + recursive printer (byte-identical, no surprises) | hand-rolled `encodeValue` (50 LOC, byte-identical) | n/a (envelope is opaque to broker; sig moves into auth) |
| **JSON Schema validation** | `jsonschema` (Draft 7+, mature) | **`ex_json_schema` Draft 4 only, ~2021 last release** — real ecosystem gap | `jsonschema::JSONSchema::compile` works but lifetime-tied; no per-decl cache yet | `jsonschema/v6` — works but generics-unfriendly | **must move to SDK** — broker has no view |
| **Reconnect / heartbeat** | manual SSE keep-alive | manual until you add Phoenix | manual | manual | free (nats-py + server-list failover) |
| **Multi-host transport story** | none today | Erlang clustering (libcluster) — native | none (would build) | none (would build) | NATS clusters / leaf nodes / gateways native |
| **Wire-format hazards** | none (Python sorts keys) | atom-interning when loading manifest module names dynamically | two `Mutex` flavors; Send-across-await footguns | map-key ordering across Go versions | broker-layer ACL denials live in `nats-server` log, not audit stream |
| **Operator footprint** | one `python3` + venv | one BEAM runtime (release tree) | one binary, no runtime | one binary, no runtime | **two daemons + persistent JS filestore** |
| **Per-call latency, p50 / p95 (loopback, 100 invokes)** | 0.674 ms / 1.621 ms | not benched | not benched | not benched | 0.586 ms / 1.058 ms |
| **Pain points** (verbatim from ANALYSIS) | hand-rolled supervisor, no per-node mailbox isolation, no native pubsub | weak JSON-Schema lib; ML ecosystem; cast-vs-call visible to new readers; atom-table risk | recursive-async + Send; SSE lifetimes; `Value` ergonomics; schema-cache lifetimes | generics half-baked; map-key ordering; error verbosity; no supervision tree; HTTP/SSE plumbing | broker can't centralise schema validation; lose unified audit; two-process ops; manifest still required |
| **Ecosystem strength for protocol work** | very strong (jsonschema, aiohttp, mature stdlib) | very strong for *concurrency* (OTP); weak for *contract validation* | very strong for safety/perf; ergonomics-tax on dynamic-typed JSON | very strong for stdlib HTTP, ops tooling; weak for actor/supervision shape | very strong for transport; you keep using your host language for everything else |

---

## 2. Per-language read

### Python (incumbent)
The Python core works, has the most tests, and is the only stack with first-class JSON Schema. Everything we'd hand-roll in another language (SSE plumbing, pending-future map, supervisor, connection table) is already written. The risk is structural: the protocol implementation is 875 lines because it's *re-implementing* primitives BEAM gives you for free. The Elixir analysis nailed it — "Colton has been writing BEAM in Python without knowing it." Every additional protocol-layer feature (per-node back-pressure, multi-host, hot-reload-without-restart) makes that gap wider.

### Elixir / BEAM
The smallest protocol implementation by a wide margin (~63% of Python at parity), and the only language where the **mesh's primitives *are* the language's primitives**: a node IS a process, request/response IS `GenServer.call`, supervision IS `DynamicSupervisor`, the admin tap IS `Phoenix.PubSub`. From a protocol-minimalism standpoint this is the strongest argument: the more the substrate gives you, the less the protocol layer has to invent, and the less surface area exists for opinion to leak in.

The real costs are honest. JSON Schema is the single hardest port — `ex_json_schema` is Draft 4 and stale, and JSON Schema is the contract format for language-agnostic nodes, so it sits squarely in the protocol layer. The BEAM cold-start cost (seconds) and resident memory floor (60–80 MB) make BEAM the wrong substrate for "spawn a fresh subprocess per request" patterns. Erlang clustering means multi-host federation is a configuration problem, not a code problem — that's a future protocol-layer win none of the others give you.

### Rust / tokio + axum
The operational champion: 5.9 MB static binary, 23 ms startup, 8.4 MB RSS, byte-identical canonical JSON, crypto in a language you sleep well with. But on the **protocol-LOC** axis Rust is roughly tied with Python, and the supervisor is the hardest 337 lines in the prototype — every restart-policy decision is a hand-rolled state machine. Lifetimes around the SSE subscriber set, Send-across-await on the supervisor's recursive respawn, and the choice between `std::sync::Mutex` and `tokio::sync::Mutex` are all real friction the Python or Elixir versions never see.

For protocol minimality, Rust pulls in the wrong direction: it forces you to spell out every restart and back-pressure policy in code, which means those policies risk getting baked into the protocol layer rather than left configurable. Where Rust is unambiguously right is on individual *nodes* — anything CPU-bound, any small edge agent, anything that has to verify signatures at line rate. The Rust analysis arrives at the same split: "Elixir is the right pivot for the core; Rust is the right tool for the nodes."

### Go 1.22+
The pragmatic middle. Goroutines + a single channel of state ops give you something close to a GenServer for ~40 lines, the stripped binary deploys with `scp`, cold start is in the noise (< 10 ms), and the race detector caught a real supervisor bug on first run. The sub-process supervisor is cleaner to write in Go than in Rust because `os/exec` + `context.Context` is just nicer than the equivalent tokio dance.

The protocol-layer cost is the same one Rust pays: there's no native supervision tree, no native pubsub, no native registry, so ~180 LOC reimplements `:one_for_one` and ~50 LOC reimplements an admin tap. Both are working code; both are also opportunities to bake assumptions into the protocol that should live in node config. Go's other protocol-shaped wart is the canonical-JSON encoder (`encoding/json` doesn't promise key order across versions across `map[string]any`), which means the wire-format compatibility check is a hand-rolled file you have to keep honest forever.

### NATS pivot (transport-layer alternative, not a language)
At 44% of Python's LOC and ~13% faster at p50, NATS is a real alternative *for the transport*. The hybrid recommendation in `PIVOT_ANALYSIS.md` is the load-bearing insight: **NATS is not a mesh, it's a transport**. The protocol value is the manifest, the typed surfaces, the schema-driven validation, and the single observable artifact — none of which NATS provides. Replacing the broker with NATS deletes 350 LOC of routing code and keeps every hard problem. NATS belongs **under** whichever language we pick for the protocol, switched on when multi-host federation arrives — not instead of a language choice.

---

## 3. Recommendation

**v1 path: Elixir. Backup: Go.**

Frame the choice through the protocol-minimalism lens. The protocol surface is envelope routing, HMAC signing, manifest-driven ACL enforcement, supervision contract, and an admin tap — and Elixir is the one substrate where every single one of those is a *language primitive* rather than a hand-rolled subsystem. A node IS a process, request/response IS `GenServer.call`, `(node, secret) → mailbox` IS Registry, restart-on-abnormal-exit IS `:transient`, the envelope tail IS PubSub. The 730-vs-1,164 LOC delta isn't a code-golf result; it's a measurement of how much "protocol" we're inventing on top of the language vs. inheriting from it. Less inventing = less protocol-layer code = less surface for product opinion to leak in. Erlang clustering also means the future "mesh spans hosts" feature is a configuration change, not a protocol change — which is exactly the property a protocol that wants to outlive today's nodes needs. The honest costs are JSON Schema (commit to a maintained fork or write a thin in-house validator that targets the slice we use; either belongs in the protocol layer and is bounded work) and the BEAM cold-start floor, which only matters if we want sub-process-per-request semantics — and that's a node concern, not a protocol concern. **Backup is Go**, not Rust: if BEAM ops complexity becomes a constraint or the JSON Schema gap proves unworkable, Go gives us a 6.7 MB static binary, < 10 ms startup, goroutine-and-channel state ownership that's close enough to the GenServer pattern, and a working JSON Schema lib — at the cost of hand-rolling ~180 lines of supervisor and accepting that the canonical-JSON encoder must be hand-maintained. Rust is reserved for high-load *nodes*, not the protocol implementation, on the explicit basis from `rust_mesh/ANALYSIS.md`: protocol LOC parity with Python plus the hardest 337 lines of the prototype is not a protocol win.

---

## 4. Five litmus tests that trigger an actual rewrite

These are the only conditions that should override "Python ships, don't rewrite." Each maps to a specific protocol-layer property the current Python implementation either lacks or can't grow into without disproportionate cost.

1. **Multi-host federation is on the roadmap and committed.** The moment the protocol must answer "machine A's nodes can talk to machine B's nodes under the same edge ACL," the cost gradient flips. Building distributed message-passing on aiohttp + service discovery + auth glue is a months-long effort that BEAM (libcluster) or NATS (clusters / leaf nodes) gives you in a config file. Trigger: a real product requirement (not a hypothetical), with a target host count ≥ 2 and a deadline.
2. **A second incident in 30 days where a node death takes the mesh down or requires manual intervention.** One incident is anecdote; two inside a month is a pattern, and it means the hand-rolled `supervisor.py` is leaking edge cases the OTP machinery has been polishing since 1986. Path A from the Elixir analysis (Elixir core, Python nodes stay over HTTP) becomes worth the weekend.
3. **Three or more nodes have hand-rolled their own restart / reconnect / supervised-loop logic.** Once the cron node, the approval node, and one more long-running daemon all reimplement "supervised work loop with backoff," we are paying the OTP tax without getting OTP — and the protocol layer is the only place that can stop the duplication. This is the smell that says BEAM was the right substrate all along.
4. **Operational requirement to ship the protocol as a single artifact with no runtime on the host** — e.g., a sidecar that drops onto an arbitrary box, an edge agent, a CI runner, a customer-installable binary. Python's `pyinstaller` story is poor enough that this is effectively a rewrite trigger. This is the case that makes **Go** the v1 path instead of Elixir, because BEAM's release tree + ERTS doesn't satisfy "no runtime."
5. **A measured, sustained protocol-layer bottleneck where Python's GIL or asyncio scheduling is the cause.** Numbers: > 5 ms p99 routing latency under any realistic load, OR > 30 % of CPU spent in the GIL on the core process under steady state. Today's measurements (sub-millisecond on loopback) are nowhere near this; if they ever get there, Rust becomes a real candidate for the *core* (not just nodes) because per-message overhead is its native strength. Without measurement, Rust is premature.

A non-trigger worth naming: "the Python code is ugly" or "I'd enjoy writing this in another language" is not a litmus test. The Python core is rarely the bottleneck and the rewrite cost is non-trivial. Premature rewrites are how interesting projects die — keep the Elixir prototype as a reference and the option open.

---

## 5. What this decision deliberately does not cover

- **Node implementation languages.** Any language with HMAC + JSON over HTTP/SSE can be a node. Rust, Go, TypeScript, shell — all valid for opinionated-layer node work. The protocol picks one language; the nodes don't have to.
- **The dashboard.** That's an opinionated-layer artifact. It can stay React + the existing Python `webui_node` regardless of what runs the protocol underneath.
- **The manifest schema and envelope shape.** Those are the protocol's wire contract and are language-independent — every prototype above proved byte-compatibility with Python's canonical JSON. The cross-language conformance test (`tests/test_protocol.py` driven against any new core) is the thing that keeps this honest, and per the Elixir analysis is the cheapest 30-minute investment with the largest option value.
- **NATS adoption.** Orthogonal to language choice and explicitly recommended as a *transport-layer* swap under whichever SDK we ship, only when multi-host federation lands.

---

## 6. Recap

The protocol's job is to stay small and unopinionated so that 100 future nodes we haven't designed can ride on it. **Elixir** wins on that axis because OTP's primitives *are* the mesh's primitives, which means the protocol implementation stays the smallest and the most generic. **Go** is the backup the moment "single static binary" becomes a hard requirement. **Rust** is the right tool for individual high-load nodes, not for the protocol layer. **NATS** is the right transport once federation matters, layered under whatever SDK we ship. **Python** stays in production until one of the five litmus tests fires — and the work to keep that option open is small (cross-language conformance test + periodic `mix test` runs against the Elixir prototype as a reference implementation).
