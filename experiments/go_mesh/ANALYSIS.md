# Go Mesh Core — Analysis

A parallel prototype of the RAVEN mesh core, written in Go 1.26 (the Homebrew
default at the time; toolchain target is 1.22+). Same scope as the Python and
Elixir prototypes: HMAC-signed envelopes, manifest YAML, JSON Schema validation,
SSE tail, an echo node, a process supervisor, and a JSON-line audit log.

## What's here

```
experiments/go_mesh/
├── cmd/mesh/main.go              CLI: `mesh {serve|echo|supervise|version}`
├── internal/crypto               canonical-JSON HMAC signing
├── internal/manifest             yaml loader + secret resolution
├── internal/mesh                 router, audit, schema cache, http+SSE
├── internal/echonode             trivial reference node
├── internal/supervisor           bounded-restart subprocess supervisor
├── manifests/demo.yaml           same shape as Python prototype's demo
└── schemas/                      copies of the shared JSON Schemas
```

Build & test:

```
$ go build ./...
$ go test ./...
ok   mesh_go/internal/crypto      0.6s
ok   mesh_go/internal/manifest    0.2s
ok   mesh_go/internal/mesh        0.4s
ok   mesh_go/internal/supervisor  0.7s
$ go test -race ./...    # also clean
```

End-to-end smoke test against `manifests/demo.yaml`: `mesh serve` boots, the
admin SSE tail streams envelopes including their `_route_status`, signing
verifies, the JSON Schema validator rejects malformed payloads, and the
process supervisor restarts a child after a non-zero exit.

## LOC comparison

| Prototype | Core file        | Total non-test LOC | Total w/ tests |
| --------- | ---------------- | ------------------ | -------------- |
| Python    | `core/core.py`   | 875 (single file)  | n/a in scope   |
| Elixir    | `lib/mesh/*`     | 730 (10 files)     | n/a in scope   |
| **Go**    | `internal/...`   | **1215** (10 files)| 1745           |

Per-concern breakdown (Go):

| Concern                          | LOC |
| -------------------------------- | --- |
| crypto (canonical JSON + HMAC)   | 141 |
| manifest (yaml + secret resolve) | 107 |
| core router (state, route, ACL)  | 322 |
| http server + SSE                | 140 |
| schema cache                     |  64 |
| audit log                        |  51 |
| echo node                        |  52 |
| supervisor (subprocess + restart)| 182 |
| CLI / main                       | 149 |

The Go core is ~65% larger than Elixir and ~40% larger than the single-file
Python core. Most of the delta is two things:

1. **Canonical JSON encoder.** Hand-rolled in 50 lines of `encodeValue` so map
   keys sort deterministically and the wire format matches Python byte-for-byte.
   Elixir paid the same tax (40 lines). Python gets it for free with
   `json.dumps(..., sort_keys=True)`.
2. **No supervision tree primitive.** ~180 LOC in `internal/supervisor` builds
   what `DynamicSupervisor` gives Elixir for ~15.

## Binary, startup, runtime memory

| Metric                | Go (this prototype)            | Elixir (BEAM)                  |
| --------------------- | ------------------------------ | ------------------------------ |
| Output artifact       | single static binary (9.8 MB)  | ~1.6 MB BEAM tree + ERTS (~80 MB resident) |
| Binary stripped       | 6.7 MB (`-ldflags='-s -w'`)    | n/a                            |
| `mesh version` time   | < 10 ms wall, ~6.6 MB peak RSS | seconds (BEAM cold start)      |
| Idle `mesh serve` RSS | ~14.6 MB                       | typically 60–80 MB             |
| Race detector         | clean under `go test -race`    | n/a                            |

The single-binary distribution story is what Go is famous for and it lives up
to it: `scp bin/mesh somehost:` is a complete deploy. No runtime, no `mix`,
no Python venv. Cold start is in the noise — practically free, which makes the
`mesh echo` subcommand a viable per-node subprocess, used by `mesh supervise`.

## Where Go was painful

**Generics still feel half-baked.** I wanted `Validate[T any](schemaPath, T)` so
the schema validator could know the payload type, but `jsonschema/v6.Validate`
takes `any` and the conversion machinery doesn't compose well with type
parameters. I gave up and threaded `map[string]any` through the whole router,
which is exactly what we'd write pre-1.18. The router's hot path
(`func(ctx, env Envelope) (map[string]any, error)`) is functionally identical
to the Python prototype's dynamic dict — the type system buys nothing here.

**Map-key ordering is a wire-format hazard.** `encoding/json` doesn't guarantee
key order across Go versions for `map[string]any`. To stay byte-compatible with
Python's `json.dumps(sort_keys=True)`, I had to hand-roll the encoder
(`internal/crypto/crypto.go`, `encodeValue`). Elixir hit the same wall and
solved it the same way. Python gets it for free.

**Error verbosity.** Every layer is `if err != nil { return …, fmt.Errorf("...
: %w", err) }`. The router (`Route`) has six error returns each ~3 lines, vs
Elixir's single `with` chain. It's not unreadable, just dense — the Go file is
60% taller than the Elixir equivalent partly because of this.

**No supervision tree.** Building `internal/supervisor` is a faithful
re-implementation of `DynamicSupervisor`'s `:one_for_one` strategy with bounded
restarts. ~180 LOC including a sliding-window restart budget. Elixir's
`DynamicSupervisor.start_child(self(), spec)` covers the same surface in the 5
lines of `Mesh.NodeSupervisor.start_node/1`. The Go version also had a
race-detector-only bug (write to `restartCount` without holding the mutex that
the read used) — the kind of bug you literally cannot write in Elixir.

**HTTP/SSE plumbing is more boilerplate than Phoenix.** `http.Flusher`-based
SSE is fine, but Phoenix.PubSub gave Elixir the tail in ~20 lines vs Go's
~50. The flush dance, heartbeat ticker, context-cancel handling — all manual.

## Where Go was natural

**Goroutines + channels for envelope routing.** The router state lives in a
single goroutine that reads `func(*coreState)` ops off a channel. That's the
Go version of "single GenServer holding mutable state" and it cost about 40
lines including `Stop()`. Subscribers each get a buffered `chan Envelope`
fanned out via `sync.Map.Range`; slow subscribers are dropped (`select
default`). Total SSE pubsub: ~30 lines. This is one of the few places Go's
concurrency model is actually crisper than the Elixir equivalent — no
explicit `Phoenix.PubSub.broadcast`, just channel sends.

**Subprocess management with `os/exec`.** `exec.CommandContext` + `cmd.Wait`
is exactly the right shape for the supervisor's run-once loop. Cancellation
propagates from the parent context, exit codes are typed, `Stdout`/`Stderr`
take any `io.Writer`. The supervisor's run loop is 25 lines of obvious code.

**Static binary deploy.** A 6.7 MB stripped binary is a real production win.
`mesh supervise manifests/demo.yaml` finds itself via `os.Executable()` and
forks copies of itself running `mesh echo <id>` — no PATH games, no Python
shebang fragility, no `mix release` build step.

**Stdlib HTTP is enough.** No framework, no DI container, no annotations.
`http.NewServeMux` + Go 1.22's `"POST /v0/route"` route syntax made the
admin server trivial. `httptest.NewServer` + `bufio.Scanner` made the SSE
test trivial.

**Race detector.** Caught the supervisor mutex bug on the first run. There is
no equivalent first-class tool in either prototype; in Python you find these
bugs in production.

## Verdict: Go vs Elixir vs Rust for this workload

Rust prototype isn't in this directory yet, so this is Go vs Elixir on
measured numbers + Go vs Rust on architectural reasoning.

**Go vs Elixir.**

The mesh core is a *workload Elixir was designed for*: long-lived stateful
processes, message passing, supervised crashes, hot code reload, a built-in
PubSub for the SSE tail. Elixir wins on every code-density and
reliability axis I can measure:

- 730 vs 1215 LOC for equivalent functionality.
- A real supervision tree with bounded-restart semantics — supervisor plus
  registry plus PubSub all in stdlib + Phoenix, vs ~180 LOC of homebrew Go.
- The "node is a process" model maps directly to GenServer + Registry; the Go
  version conflates handlers (functions) with declarations (data) and reaches
  back through the registry to find the secret.
- Crash isolation: an Elixir node's `raise` is caught by the supervisor and
  the rest of the mesh keeps going. The Go in-process echo node's panic
  inside a handler goroutine would crash the whole server unless you
  `defer recover()` in every dispatch — which I haven't done.

Where Go beats Elixir:

- **Distribution.** One stripped binary. No Erlang VM, no dependency tree.
- **Cold start.** Sub-10ms vs BEAM's seconds. Makes Go viable for
  per-invocation CLI tools (the `mesh echo` subprocess) in a way BEAM is not.
- **Memory floor.** ~15 MB for an idle server vs ~70 MB for an idle BEAM node.
- **Static analysis.** `go vet`, `go test -race`, the type checker. BEAM's
  story is dialyzer + tests.

For *this* workload — a small cluster of long-lived, supervised, message-passing
nodes with a tail stream — Elixir is the right tool. The supervision tree, the
"each node is a process," and the PubSub-as-a-feature compound. Go feels like
re-implementing OTP one chunk at a time.

**Go vs Rust (predicted).**

I have not measured this yet, but architecturally:

- Rust will be 2x the LOC of Go again, mostly fighting the borrow checker
  through the router's shared state. The "single goroutine owns state +
  channel of ops" pattern in Go would translate to a `tokio::sync::Mutex` or
  an actor crate (`actix`, `tokio` channels), and either way you pay
  lifetimes everywhere `Envelope` flows through async fns.
- Rust will produce a smaller, faster binary (~3 MB stripped) with lower
  steady-state memory (~5 MB). For this workload, the ceiling isn't CPU or
  memory — it's process count and message latency, neither of which Rust
  meaningfully helps with at the scale this mesh runs.
- Rust's type system *would* let us replace `map[string]any` envelopes with
  typed `Envelope<P: PayloadKind>`, which is the one place Go's lack of
  expressive generics hurts. But the wire format is dynamically typed JSON
  anyway, so this is a developer-ergonomics win that doesn't propagate to
  the network boundary.

**Honest summary.** If we ship one of these to production:

1. **Elixir** if I want the easiest correct implementation and we can swallow
   the BEAM deploy story (and we can — the mesh sits next to other RAVEN
   processes that already need a runtime).
2. **Go** if the mesh has to be a sidecar binary that drops onto an arbitrary
   host with no runtime — the deployment story is meaningfully better and the
   code is competitive in size/perf, even if it lacks supervision-tree
   ergonomics.
3. **Rust** only if we discover a specific bottleneck (not yet observed) where
   Go's GC pauses or Elixir's BEAM overhead actually matter. For a mesh that
   routes envelopes between supervised nodes, this is unlikely.

For RAVEN's current trajectory — small mesh, owner-only, runs alongside
other Elixir/Python processes — **Elixir wins**. Go is the strong runner-up
and the right answer if "single binary" becomes a hard requirement.
