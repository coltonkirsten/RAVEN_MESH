# PROTOTYPE — running the Python Core, and how it's structured

This is the Python reference implementation of [the protocol](./PROTOCOL.md). It is deliberately small, deliberately single-process, deliberately easy to throw away when the BEAM refactor lands.

## Run it

```bash
# from repo root
pip install aiohttp pydantic pyyaml jsonschema croniter structlog pytest pytest-asyncio
scripts/run_demo.sh start          # boots Core + dummy tasks + dummy approval
scripts/run_full_demo.sh start     # boots Core + all four real nodes
```

Stop with the matching `stop` subcommand. Logs land in `.logs/`, pids in `.pids/`, audit in `audit.log`.

The dashboards are:

| node | url | what it shows |
| ---- | --- | ------------- |
| webui_node    | http://127.0.0.1:8801 | latest message + colored panel; updates live via SSE |
| human_node    | http://127.0.0.1:8802 | inbox feed; form to invoke any allowed surface |
| approval_node | http://127.0.0.1:8803 | pending approval cards with Approve / Deny buttons |

Core itself exposes:

| endpoint | purpose |
| -------- | ------- |
| `GET /v0/healthz`     | liveness + counts |
| `GET /v0/introspect`  | declared nodes, edges, current connection state |

## Layout

```
core/
  core.py          # the entire single-process Core. ~430 lines.
node_sdk/
  __init__.py      # MeshNode helper that hides HMAC + SSE + envelope plumbing
nodes/
  dummy/           # protocol-test dummies (actor, capability, approval, hybrid)
  cron_node/       # hybrid; persists schedules to data/crons.json
  webui_node/      # capability with browser dashboard on :8801
  human_node/      # actor with browser dashboard on :8802
  approval_node/   # approval with browser dashboard on :8803
schemas/           # JSON Schemas referenced by the manifests
manifests/
  demo.yaml        # protocol-validation demo (used by tests/)
  full_demo.yaml   # all four real nodes wired up
scripts/           # bash run_*.sh wrappers (set env, exec module)
tests/             # pytest suite — protocol conformance + envelope edge cases
docs/              # PROTOCOL.md (language-agnostic), this file
```

## Core internals

`core/core.py` is one file with a clear shape:

1. **`canonical / sign / verify`** — HMAC-SHA256 helpers. Same canonicalization rule as `node_sdk` so signatures match by construction.
2. **`CoreState`** — the in-memory registry. Holds:
   - `nodes_decl[node_id]` — declared kind, secret, surfaces (with parsed schemas), metadata.
   - `connections[node_id]` — live session id and the SSE delivery `asyncio.Queue`.
   - `sessions[session_id]` — reverse lookup, used by `/v0/stream`.
   - `edges` — set of `(from, to_surface)` tuples.
   - `pending[msg_id]` — outstanding request/response futures, with the target node so a response from anywhere else gets rejected.
3. **Handlers** — one per HTTP route. Each one is a straight-line function: parse, verify signature, look up edge, validate schema, emit audit event, queue or block.
4. **`make_app`** — wires routes, registers an `on_shutdown` hook that closes every active SSE queue so `web.AppRunner.cleanup()` returns instantly during tests.

Concrete invariants worth knowing:

- A response is accepted only from the node that originally received the matching invocation. This means an approval node's reply (which carries `from: approval_node`) only resolves the original `voice → approval.inbox` call — exactly what we want.
- `_close` is a sentinel queue event used to evict the previous SSE stream when a node re-registers.
- The audit log path is configurable via `AUDIT_LOG`. Tests redirect it to a per-test temp file.

## Test suite

```bash
python3 -m pytest -v
```

`tests/test_envelope.py` covers HMAC sign/verify behavior and JSON Schema edge cases. `tests/test_protocol.py` boots Core in-process via `web.AppRunner` and exercises all ten flows from PRD §7 — including a "no-SDK" external node that speaks the protocol with hand-rolled stdlib HTTP. If you change the wire protocol and these still pass, you didn't actually change it.

## How nodes talk to Core (the SDK contract)

`node_sdk.MeshNode` is a thin wrapper. The two-phase startup matters:

```python
node = MeshNode(node_id, secret, core_url)
await node.connect()         # POST /v0/register; populates node.surfaces and node.relationships
node.on(surface_name, fn)    # register handlers AFTER seeing declared surfaces
await node.serve()           # opens GET /v0/stream and starts dispatching
```

Handler return values:

| return | sent as | when |
| ------ | ------- | ---- |
| `dict` | `kind="response"` | normal request/response |
| `None` | nothing | `fire_and_forget` inboxes |
| `raise MeshDeny(reason, **details)` | `kind="error"` | explicit denial |
| any other exception | `kind="error"` with `reason="handler_exception"` | safety net |

Sending invocations:

```python
await node.invoke("tasks.list", {})                     # blocking request/response
await node.invoke("user.inbox", {...}, wait=False)      # fire_and_forget
await node.invoke("downstream.x", inner, wrapped=env)   # approval forwarding
```

## How to refactor away from this Python implementation

The protocol is the contract; this prototype is a conformance test. When BEAM/Elixir Core lands:

1. **Don't change the wire protocol.** Core's only job is to serve the same HTTP/SSE endpoints with the same envelope semantics. If you rewrite `core/core.py` in Elixir but keep `tests/test_protocol.py` passing (you'd run it against the new Core's bound port), the protocol is preserved.
2. **The SDK lives.** `node_sdk` and every node implementation can keep running unchanged; they only know about `core_url` and the protocol.
3. **What gets thrown away** is `core/core.py` — the in-memory registry, the asyncio queues, the audit-line writer. Replace with `Registry` (Horde), per-session GenServers, an audit GenServer + log compaction. The mapping is straightforward because the Python file already groups the responsibilities cleanly.
4. **Conformance bar:** the new Core passes `tests/test_protocol.py` (port-pointed at it) and the manual demo flows behave identically.

## Known caveats

- HTTP-on-localhost only in this build. Tailscale + TLS is documented in PROTOCOL.md and PRD §5.2 but not wired in here. The protocol is unchanged either way.
- Audit log writes are O(N) line appends with an asyncio lock. Fine for prototype scale; replace with a buffered/aggregator node when needed.
- No reconnect logic. If a node's SSE drops, it should call `node.connect()` again — there's no automatic resume.
- `nodes/cron_node/data/crons.json` is local on disk. If you move the cron node to another host you lose the schedules.
