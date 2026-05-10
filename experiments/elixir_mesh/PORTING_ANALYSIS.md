# RAVEN_MESH Porting Analysis: Python → Elixir/BEAM

**Author:** Elixir prototype worker, working off the v0.4 Python core
at `/RAVEN_MESH` and the working prototype at `experiments/elixir_mesh/mesh/`.
**Status of the prototype:** runs (`mix run bin/demo.exs`), 12 tests
pass (`mix test`). Demonstrates manifest load, signature
verification, edge ACL routing, fire-and-forget + request/response,
hot-add of new nodes without restart, supervisor crash recovery, and
a PubSub envelope tail.

---

## 1. What's natural in BEAM that's painful in Python

These are the wins I actually felt while porting, not theoretical
ones.

### 1.1 Per-node state lives in its own process for free

Python: every node ID maps to a `connections[node_id]` dict entry
holding an `asyncio.Queue`, a session_id, a connected_at timestamp.
The node's *own* state lives across an HTTP boundary in a separate
Python process that re-registers when it crashes — and the core has
to keep a `pending: dict[str, dict]` of in-flight `asyncio.Future`s
to correlate responses (`core.py` lines 79-90, 280-306).

Elixir: the node *is* a process. Its state is a GenServer field.
There is no connection table, no session, no pending-future map. A
GenServer.call already gives request/response with a timeout. The
entire pending-future bookkeeping (`core.py` lines 280-306,
`handle_respond` lines 316-340) collapses to ~10 lines in
`core.ex` lines 175-194.

```python
# Python core: just the pending-future bookkeeping
fut = asyncio.get_event_loop().create_future()
state.pending[msg_id] = {"future": fut, "target_node": target_node, "from_node": from_node}
await target_conn["queue"].put(deliver_event)
try:
    result = await asyncio.wait_for(fut, timeout=timeout)
finally:
    state.pending.pop(msg_id, None)
```

```elixir
# Elixir core: same semantics, no bookkeeping
case GenServer.call(pid, {:invoke, env}, 30_000) do
  {:ok, payload} -> {:ok, payload}
  {:error, reason, details} -> {:error, reason, details}
end
```

### 1.2 Crash recovery requires no code

The Python "what happens when a node dies" story is: SSE stream
disconnects, queue is orphaned, on next register the old session is
torn down (`core.py` lines 189-203). It works, but the recovery
path is *coded by hand*, and a node's internal state is never
restored — that's also true in Elixir, but in Elixir it happens via
a 1-line `restart: :transient` flag and the supervisor.

The Elixir crash test (`test/mesh_test.exs:118-134`) literally is:

```elixir
Process.exit(old_pid, :kill)
:ok = wait_for_restart("kanban", old_pid)
{:ok, %{"card" => _}} = Mesh.invoke("voice_actor", "kanban.add_card", %{...})
```

Twelve lines. The supervisor restarts, the registry re-points, the
next call to `Mesh.invoke` resolves to the new pid. There is no
"reconnect logic" anywhere because there is no connection.

### 1.3 Hot-add of a new node is a two-line API

Python: edit YAML, save, POST `/v0/admin/reload`, the new node has
to come up as a separate subprocess and reach the core to register
(`core.py:_resolve_secret`, scripts/run_full_demo.sh). The "running
node count" is decoupled from the "declared node count" until that
external process succeeds in registering.

Elixir: `Mesh.add_node(decl, edges)` calls
`DynamicSupervisor.start_child` (`core.ex:84-94`,
`node_supervisor.ex:23-37`). 7 lines. The new node is *guaranteed*
to be alive when the call returns — the supervisor either started
it or surfaced the error.

### 1.4 The "envelope tail" is one Phoenix.PubSub call

Python `handle_admin_stream` (`core.py:457-496`) is 40 lines of
`web.StreamResponse`, queue allocation, heartbeat-on-timeout
plumbing, and exception handling for `ConnectionResetError /
BrokenPipeError`. The Elixir equivalent (`tail.ex`) is 21 lines
total, of which 8 are docstring. That's not a fair fight — SSE-over-
HTTP is intrinsically more code than in-process pubsub — but it's a
real concrete saving once the eventual rewrite picks an HTTP
framework. With Phoenix, you'd add a Phoenix.Channel and get
WebSocket+SSE+long-poll fanout with one more line.

### 1.5 Line counts on the parts that ported

| concern                          | Python (core.py)        | Elixir          |
| -------------------------------- | ----------------------- | --------------- |
| HMAC + canonical JSON            | ~20 lines               | 82 lines (much of it iodata builder + docstring; behaviour is identical) |
| state shape                      | 90 lines (CoreState)    | 6 lines map     |
| invocation routing (no transport)| 90 lines (`_route_invocation`) | 60 lines (`do_route` + helpers) |
| pending-response bookkeeping     | ~30 lines               | **0** (GenServer.call) |
| node lifecycle + register / disconnect | ~70 lines         | **0** (DynamicSupervisor + Registry) |
| envelope tail (admin stream)     | 40 lines SSE plumbing   | ~5 lines PubSub call sites |

Total Python (`core/core.py` + `node_sdk/__init__.py`) = **1,164 lines**.
Total Elixir prototype across `lib/mesh/` = **730 lines**, *and that
includes the demo node implementations* (Echo + Kanban) that Python
counts elsewhere in `nodes/`. The behaviour-equivalent core is
roughly 40-50% the size at feature parity, and the remaining lines
are mostly boilerplate (module docstrings, `defp` glue) rather than
load-bearing logic.

### 1.6 `:transient` restart + `max_restarts: 10, max_seconds: 5`

This is the clean-shutdown-vs-actual-bug discrimination Erlang has
had since 1986. A node that calls `System.stop/0` (intentional
shutdown) stays down. A node that crashes gets restarted up to N
times before the supervisor itself gives up and propagates upward.
Python's only equivalent is a watchdog process you write yourself.

---

## 2. What's painful in BEAM that's natural in Python

Being honest. There is friction.

### 2.1 JSON-Schema validation is much weaker

Python uses `jsonschema.validate` (Draft 7+, mature, ubiquitous,
correct) — `core.py:271` is one line. Elixir options:

- `ex_json_schema` — Draft 4 only, last meaningful release ~2021
- `json_xema` — newer, smaller community
- Hand-roll per-surface validation modules

This is a real ecosystem gap. If the protocol stays
JSON-Schema-shaped, the Elixir port either commits to maintaining
a fork of `ex_json_schema` or trades JSON Schema for a different
contract format (Protobuf, Norm, Ecto.Changeset). I'd lean toward
keeping JSON Schema and accepting the dep risk, because the Schema
files are the contract for *language-agnostic* nodes.

### 2.2 ML / model-serving ecosystem

The Python core is plain HTTP, but the *interesting* nodes —
nexus_agent, voice_actor — call out to Anthropic, OpenAI,
Whisper/ElevenLabs, possibly local LLMs via vLLM. Elixir's options:

- `:anthropic` Hex package exists but is community-maintained and lags
- Bumblebee / Nx for local models — real, but the model coverage is
  narrower than HuggingFace+transformers
- HTTP client to Python sidecars works but defeats the purpose

If most "smart" nodes will continue to call cloud LLMs over HTTP,
this is a non-issue (Req + Jason is fine). If we want to run models
locally, Python's lead is large enough that you'd want to keep those
nodes in Python regardless.

### 2.3 `async/await` ergonomics for one-off scripts

This came up writing the demo. Python's `asyncio.run(main())` is
trivial; you sprinkle `await` and call it a day. Elixir doesn't have
that ergonomic layer — you `GenServer.call` for sync, `Task.async`
for fire-and-forget, `Stream.repeatedly` if you want a tail. None of
those are bad, but they're more *visible*. A new contributor reading
`bin/demo.exs` has to know the difference between cast and call;
they don't have to know the difference between `await` and "no
await" because that's the same syntax. **For a glue codebase
maintained by one person, this matters.**

### 2.4 String/binary boundary and atom interning

`String.to_existing_atom("Elixir." <> name)` (`manifest.ex:78`)
exists because dynamically-loaded module names from a manifest are
attacker-controlled. Atom table exhaustion is a real BEAM concern
that has no Python equivalent. It's a small thing — but it surfaces
the moment you load configuration from disk into running code.

### 2.5 Tooling for one-off introspection during development

`python3 -i core.py` + an interactive REPL that already has all
modules loaded is hard to beat. `iex -S mix` is *better* (live
process inspection, `:observer.start()`, hot code reload), but it's
a steeper first 30 minutes for someone who isn't already an Erlang
person.

### 2.6 Type checker warnings on macro-injected code

The prototype ships with two harmless typing warnings on the
`use Mesh.Node` macro because Elixir 1.19's gradual typer correctly
narrows the dispatch return type per implementing module and flags
unreachable clauses. It's diagnostic-quality output, but it's noisy.
Python's mypy/pyright on this codebase would either silently accept
or be tuned to ignore this whole class.

---

## 3. Migration strategy

Three viable paths. Ordered from cheapest to boldest.

### Path A: Elixir core, Python nodes stay (HTTP at the boundary)

Keep the protocol. Replace `core/core.py` with the Elixir core +
HTTP layer (`Plug.Cowboy` + the existing `/v0/*` endpoints). Python
nodes register over HTTP, sign with HMAC, consume SSE — none of
their code changes.

**What the protocol boundary needs:**

- `POST /v0/register` — already covered, just needs Plug routing
- `POST /v0/invoke` — Core verifies sig, calls `do_route`
- `POST /v0/respond` — Core resolves the in-flight GenServer.call
  via a `pending: %{}` map (this is the one place Python's
  bookkeeping resurfaces, because the responder is across HTTP)
- `GET /v0/stream` — translate the per-node mailbox into SSE; the
  Python node SDK reads it without modification
- Canonical JSON byte compatibility — **already verified** in this
  prototype's `crypto.ex`

**Cost:** ~500-700 lines Elixir to add the HTTP layer. **Risk:**
low. The Python-side test suite (`tests/test_protocol.py`) is the
conformance test — if all 19 tests pass against the Elixir core,
the boundary is preserved.

**This is the path I'd ship first.** It gives BEAM's wins where they
matter (supervision, hot-add, crash recovery, pubsub tail) without
touching the Python nodes that actually do the work.

### Path B: Elixir core + Elixir SDK, both Python and Elixir nodes coexist

Same as A, plus an Elixir node SDK (the Mesh.Node behaviour in this
prototype, plus a thin HTTP MeshNode for nodes that run out-of-tree).
New nodes are written in Elixir; old ones stay Python. The protocol
is still HTTP.

**Cost:** A + ~200 lines for an HTTP-speaking MeshNode in Elixir.
**Risk:** low. **When:** as soon as a new node is written that's
heavily concurrent (cron scheduler, fan-out broadcasters,
long-running background work). Cron in particular wants to be a
GenServer with `:erlang.send_after/3`, not an aiohttp loop.

### Path C: All-in rewrite, all nodes are Elixir

Only worth doing for nodes that don't depend on the Python ML
ecosystem. The kanban-style nodes, cron, approval, webui, human
dashboard — all natural in Elixir/Phoenix LiveView (LiveView would
in fact *erase* the dashboard subsystem; the webui_node becomes
~50 lines instead of a separate aiohttp app).

But **don't rewrite voice_actor or nexus_agent**. Those should call
out to Python or to LLM HTTP APIs directly from Elixir. Rewriting
LangChain-equivalent orchestration in Elixir is a tar pit.

**Cost:** weeks of work. **Risk:** medium-high if undertaken in one
shot. **Trigger:** a clear case where the Python node count exceeds
~10 and the supervision/concurrency complexity is biting (e.g.
"we're seeing race conditions in cron + approval + webui all
talking to each other and the connection table is unstable").

### My recommended sequencing

1. **Now:** keep this prototype around as a reference. Do not rewrite.
2. **When you have ≥ 5 long-running nodes that don't talk to LLMs:**
   take Path A. Replace core/core.py with this Elixir core +
   plug_cowboy. ~1 weekend of work given the prototype.
3. **When a new heavily-concurrent node appears that's awkward in
   Python:** add it as the first Elixir node (Path B). Use this as
   the proof point for the rest.
4. **Path C only if** you find yourself maintaining duplicated
   "supervised reconnect logic" across multiple Python nodes. That's
   the smell that says BEAM was the right substrate all along.

---

## 4. Surprising findings

Things I didn't expect.

### 4.1 The Python core is more BEAM-shaped than I thought

Reading `core.py` cold, I expected to find object-oriented spaghetti.
Instead the structure is already actor-shaped: there's a
`CoreState`, every interaction is a message into an `asyncio.Queue`,
every node is identified by a string ID with a per-node mailbox.
**Colton (or whoever wrote this) has been writing BEAM in Python
without knowing it.** The port isn't a translation, it's a
*decompression* — the same logical structure with the BEAM-shaped
parts (mailbox, supervision, registry) replaced by their native
primitives. That makes Path A genuinely cheap.

### 4.2 The pending-future map is the only hard transport detail

Most of `core.py` ports trivially. The one place that resists is
`handle_respond` — when a Python node responds *across HTTP* to a
request that was made by `POST /v0/invoke`, the core has to find the
in-flight Python `Future` it created and resolve it. In the in-
process Elixir prototype, this is free. In an HTTP-fronted Elixir
core (Path A), you re-introduce a `pending: %{msg_id => from}` map
in Core, and `handle_respond` does `GenServer.reply(from, payload)`.
That's 15-20 lines, not 30, and it's the only piece of Python state-
keeping that survives the port.

### 4.3 Phoenix.PubSub solves admin tap *and* future fan-out

The Python admin stream is a one-way tap. Phoenix.PubSub gives you
the same thing for ~5 lines, *and* gives you topic-based fan-out for
free. If the mesh ever wants "all subscribers to envelopes from
node X" or "all subscribers to surface foo.bar", that's a topic
naming convention away. Python would require Redis or a custom
fan-out broker.

### 4.4 JSON-Schema validation is the single hardest thing to port

I expected SSE or HMAC to be the friction point. They weren't —
`:crypto.mac/4` is wire-compatible with Python's hmac in 4 lines.
The actual friction is JSON Schema. If we keep it as the contract
format, we're committing to a less-mature Elixir library or a fork.
If we drop it, we lose the language-agnostic story.

### 4.5 The "external_node" demo in tests/test_protocol.py is the
**real** spec

That test exercises a node that does HTTP + HMAC + SSE with stdlib
only, no SDK. It's the conformance contract for the whole protocol.
**Any Elixir core port should run that exact Python test against
itself.** If it passes, the port is correct. The test is short
enough (~30 lines) that you could even copy it into the Elixir
repo's `test/` and shell out to `python3` to drive it.

### 4.6 `restart: :transient` + DynamicSupervisor solves the
"intended vs unintended shutdown" problem that Python papers over

In Python, when a node process exits cleanly (you hit Ctrl-C), the
core sees the SSE disconnect and treats it the same as a crash.
There's no in-band signal of "I meant to leave". In Elixir,
`:transient` means *restart only on abnormal exit* — clean
`System.stop/0` exits stay down, while `Process.exit(pid, :kill)`
restarts. This distinction is subtle but important once you have
nodes that explicitly tear themselves down (e.g. cron after firing
a one-shot).

---

## 5. My honest recommendation

**Don't rewrite now.** But keep this prototype.

The Python core is 875 lines. It works. It has 19 passing tests.
The cost of the rewrite — even Path A, the cheapest path — is
non-zero, and the marginal user-visible improvement at the current
node count (≤10 nodes, all single-machine, no external pressure on
fault tolerance) is small. The supervision wins look great in a
demo, but the Python core is rarely actually crashing. Premature
rewrites are how interesting projects die.

**The trigger event that says "now":**

One of three things, whichever happens first:

1. **Node count ≥ 10 *and* multi-process supervision is hand-rolled
   in three or more places** — that's the smell that says you've
   accidentally re-implemented OTP poorly. The cron node, the
   approval node, and any new long-running daemon will all want
   their own supervisor; once you've written that pattern by hand
   three times, you're paying the OTP tax without getting OTP.

2. **A real fault-tolerance incident** — a node dies in a way that
   takes the mesh down for non-trivial time, *or* requires manual
   intervention to recover. One incident is anecdote; the second
   incident inside 30 days is the trigger. Path A becomes worth a
   weekend.

3. **You hit a feature wall** — specifically, distributed mesh (mesh
   spans multiple machines), where you'd want Erlang clustering. If
   the design starts requiring "machine A's nodes can talk to
   machine B's nodes via the same edge ACL," you should do this on
   BEAM, not on aiohttp + service discovery + auth glue. You will
   save months.

**If none of those happen, the rewrite is a hobby project, not
infrastructure work.** Let it stay a hobby project until it's not.
The Elixir prototype in this directory is exactly enough to *prove*
the rewrite is feasible and roughly half the size. Re-running
`mix test` periodically (after meaningful protocol changes to the
Python core) keeps it honest as a reference. If/when one of the
three triggers fires, this prototype is the head start, not the
finished work.

**One concrete thing I'd do regardless of the rewrite decision:**
copy the canonical-JSON test from `crypto.ex` into the Python repo
as a cross-language conformance test (drive a Python test with a
golden vector that the Elixir module produces). That keeps the wire
protocol from drifting and makes Path A trivial whenever it
happens. Cost: 30 minutes. Value: the option stays open forever.
