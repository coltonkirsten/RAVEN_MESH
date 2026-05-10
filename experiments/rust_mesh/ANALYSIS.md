# Rust + tokio prototype — RAVEN_MESH ALTERNATIVE-CORE

A working tokio + axum + tower implementation of the same v0 wire protocol
as the Python `core/core.py` and the Elixir `experiments/elixir_mesh/`
prototype. Built to compare ergonomics and runtime cost honestly.

```
experiments/rust_mesh/
  Cargo.toml
  src/
    main.rs        - clap subcommands: `core` and `echo`
    lib.rs         - module re-exports for integration tests
    canonical.rs   - canonical-JSON + HMAC-SHA256 sign/verify
    manifest.rs    - YAML manifest loader, schema-file resolution
    state.rs       - Core handle: Arc<RwLock<CoreInner>> + audit + supervisor
    router.rs      - axum routes incl. SSE delivery and admin tap
    supervisor.rs  - per-child tokio monitor task with backoff
    echo.rs        - `rust_mesh echo` subcommand
    audit.rs       - JSON-per-line audit log
  tests/integration.rs  - 6 cargo tests (built-in protocol coverage)
  manifests/test.yaml + echo.json
```

Build & test:
```
cargo build --release    # 26.4 s clean
cargo test               # 6 / 6 pass in ~1.8 s after compile
./target/release/rust_mesh core --manifest manifests/test.yaml --port 8000
./target/release/rust_mesh echo --core-url http://127.0.0.1:8000 --node-id bob --secret bob-secret
```

End-to-end demo confirmed: an admin-synthesized invocation
`alice → bob.ping` is canonical-signed, routed, schema-validated, delivered
over SSE to the in-process echo node, and the response comes back
through `/v0/respond` with a fresh signature that verifies.

## 1. Lines of code (equivalent functionality only)

| Surface | Python | Rust |
|---|---:|---:|
| Core wire protocol + admin (`core.py` ↔ `state.rs` + `router.rs` + `canonical.rs` + `audit.rs` + `manifest.rs`) | 875 | ~1024 |
| Process supervisor (`supervisor.py` ↔ `supervisor.rs`) | 474 | 337 |
| Echo node (Python lives in `nodes/`, not in core; here it's `echo.rs`) | n/a | 105 |
| CLI / bootstrap | (in `core.py`) | 72 (`main.rs`) |
| Tests | tests/test_protocol.py (~600) | tests/integration.rs (293) |

Apples-to-apples (excluding echo + tests): **Python 1349 LOC vs Rust 1361 LOC.**
Roughly the same — Rust's win on the supervisor (no opaque OS-signal
plumbing needed because tokio's `Child` already exposes `.wait()` /
`.kill()`) is canceled by axum's extractor + `IntoResponse` boilerplate
on every handler.

## 2. Build / size / runtime measurements

Both servers booted with their respective manifests, polled `/v0/healthz`
until 200, then `ps -o rss`. macOS arm64, M-class, warm filesystem cache.

| Metric | Python (aiohttp) | Rust (axum) |
|---|---:|---:|
| Clean release build | n/a | 26.4 s (`cargo build --release`) |
| Binary / artifact size | (interpreter + ~12 MB site-packages) | **5.9 MB** (single static-ish binary, LTO + strip) |
| Startup → first 200 healthz | **150 ms** (4-node manifest) | **23 ms** (2-node manifest) |
| Idle RSS | **47 MB** | **8.4 MB** |
| Test suite runtime | ~3 s | 1.8 s |
| Cold cargo build (incl. dep compile) | n/a | ~58 s first run |

Rust is ~6× faster to start and uses ~5.6× less memory, and ships as one
file you can `scp` to a box that has no Python at all. That's the only
operationally compelling number in the set.

## 3. Where Rust was painful

**Recursive async + Send.** First version of the supervisor mirrored
`supervisor.py` shape: monitor task awaits child exit, then on
unexpected exit calls `self.start(spec).await` to respawn. Compiler
rejected because `start()`'s future is not `Send` when it itself
recursively awaits inside `tokio::spawn`. Fix was a structural rewrite
to a single per-child `monitor_loop` that owns its spec and re-enters
the spawn step in a `loop`. Better design in the end, but it cost
~30 minutes of fighting "future created by async block is not `Send`"
errors I could not point at any single offending line for.

**Lifetimes for SSE subscriber state.** Each admin SSE subscriber needs
a sender half stored in `CoreInner` and a receiver half held by the
SSE response stream. Python: `_admin_streams: set[asyncio.Queue]` and
push directly. Rust: a `Vec<mpsc::Sender<Value>>` plus a `try_send`
fanout that prunes dead senders, *and* the SSE handler has to
`async_stream::stream!` over a moved receiver because axum's
`Sse<S>` requires a `Stream` with `'static` lifetime. The channel
wiring is fine but doubles the line count vs. Python's "stash the
queue, write to it" pattern.

**JSON ergonomics.** `serde_json::Value` works but accessing nested
fields is verbose: `env.get("payload").and_then(|v| v.as_str())`.
Strongly-typed envelopes via `#[derive(Deserialize)]` would be cleaner
but the wire protocol is intentionally loose-typed (arbitrary
payloads), so I kept `Value` and paid the ergonomics tax. Python's
`env.get("payload", {})` is a single call.

**Compiled-on-demand JSON Schemas.** `jsonschema::JSONSchema::compile`
is called on every invocation. To cache compiled validators in the
`NodeDecl`, the compiled type's lifetime is tied to the source
`Value` — so I'd need `Arc<Value>` storage and either an Arc-based
schema or owning copies. Doable but I deferred it; the prototype
recompiles per call. Python's `jsonschema.validate(payload, schema)`
hides this entirely.

**Two `Mutex` flavors.** I confused `std::sync::Mutex` and
`tokio::sync::Mutex` once and got a compile error about holding a
non-`Send` guard across an await. Easy fix once spotted but it's the
kind of footgun a Python dev wouldn't even know to look for.

## 4. Where Rust was natural

**HMAC + canonical JSON.** `hmac` + `sha2` + a tiny recursive printer
gave byte-for-byte output identical to Python's `json.dumps(..., sort_keys=True, separators=(",", ":"))`.
Verified via the `signature_round_trip_matches_python_canonical` test.
This is the single safest piece of code in the prototype — no async,
no allocation surprises, no GC pause. You sleep well at night when the
crypto is in Rust.

**Process supervision.** `tokio::process::Command::spawn()` →
`Child::wait()` → `tokio::select!` between exit and stop signal. The
whole "is this child still alive" loop is 25 lines and has no
zombie-reaping, no SIGCHLD, no fd-limit babysitting. The Python
supervisor needs `start_new_session=True` + manual `os.killpg` because
asyncio's subprocess primitives are awkward; tokio's are just nice.

**Cancellation.** `tokio::select!` between `child.wait()` and
`stop_rx.changed()` is the cleanest way to model "do A until B
happens" I've ever written. The Python equivalent is
`asyncio.wait_for(future, timeout)` with `CancelledError` re-raises
sprinkled defensively.

**Strict route handlers.** `Json(body): Json<Value>` either parses or
returns a 400 before my code runs. Python writes
`body = await request.json()` and you get to handle `JSONDecodeError`
yourself. axum's extractor pattern moves a class of bug out of user
code.

**One binary, two roles.** `main.rs` ships both the core *and* the
echo node as subcommands of the same release binary. That was the
single nicest part of the experience for a deployable mesh — no
"is the right Python/uv installed on the target" worry.

## 5. Honest verdict — Rust vs. Elixir for the pivot

Both prototypes work; both pass equivalent tests. Pick by what the
mesh's hardest problem actually is.

**Rust wins if the bottleneck is operational footprint or per-message
overhead.** 8 MB RSS, 23 ms cold start, single static binary, no
runtime to install. If the mesh ever needs to fan out to small edge
boxes, container slots, or a dashboard that boots subprocess nodes on
demand, those numbers compound. Crypto stays fast and correct.

**Elixir wins if the bottleneck is supervision shape and the node
graph itself.** OTP's DynamicSupervisor + Registry + GenServer model
*is* the mesh — `start_node` / `stop_node` are five-line functions
that get crash recovery, intensity throttling, and message-passing
back-pressure for free, because they run on machinery the BEAM has
been polishing for 30 years. The Rust supervisor I wrote works but
re-implements a subset of OTP's restart semantics in 337 lines of
hand-rolled state machine. Every escalation policy I'd want next
(one-for-all, rest-for-one, restart intensity per group) is another
chunk of bespoke logic.

**My recommendation:** Elixir is the right pivot *for the core*. The
mesh is fundamentally a supervision tree of message-passing actors,
which is exactly what BEAM was built for. Rust is the right tool for
the *nodes* — anything CPU-bound, anything that needs a small static
binary at the edge, anything that has to verify signatures at line
rate. A mixed deployment (Elixir Core + Rust nodes wired over the
same v0 protocol) keeps each tool inside its comfort zone, and the
HMAC/canonical-JSON envelope is portable enough that this isn't
hypothetical: this prototype's `echo` subcommand already speaks
the same wire protocol the Elixir core would expose.

The single-language case for Rust is an operational one (one binary,
small footprint), not an ergonomic one. The supervisor was the
hardest 337 lines of the prototype, and the Elixir version is 50
lines for an arguably more correct implementation.

## 6. Test coverage

`cargo test` runs 6 integration tests, all green:

1. **`signature_round_trip_matches_python_canonical`** — HMAC sign +
   verify, tampering invalidates, wrong secret rejects.
2. **`manifest_load_parses_nodes_and_edges`** — YAML + per-surface
   schema file resolution.
3. **`envelope_routing_request_response_via_http`** — full round-trip:
   register → SSE → invoke → respond → reply matches.
4. **`envelope_routing_rejects_no_relationship`** — ACL enforcement
   (`from`/`to` not in edges → 403).
5. **`supervisor_restarts_crashed_child`** — spawns a script that
   exits 1, observes ≥2 invocations within 1.5s.
6. **`admin_sse_broadcasts_envelope_tail`** — admin tap subscriber
   receives an envelope event after a routed (denied) invocation.

The tests required no mocks — they bind a fresh axum server on a
random port and speak real HTTP / SSE.
