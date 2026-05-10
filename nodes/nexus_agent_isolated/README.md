# nexus_agent_isolated — RAVEN Mesh node

A sibling of `nexus_agent` that runs the `claude` harness **inside a Docker
container**. The agent is fully isolated from the host filesystem — it sees
an empty `/workspace`, has no view of any host code, and reaches the mesh
only through the same MCP bridge / control-server pattern as the host
version.

## Why a separate node?

`nexus_agent` runs `claude` as a subprocess on the host. Convenient, but the
agent sees host files and shares the host's environment. `nexus_agent_isolated`
is the answer when you want a sandboxed agent: same wiring, same tools, same
ledger — but the binary executes inside Docker.

## Architecture

```
┌──────────── HOST PROCESS (python) ────────────┐
│                                                │
│   agent.py    — registers with Core,           │
│                 owns the inbox handler         │
│   web/        — inspector UI (8806)            │
│   make_control_app — loopback HTTP (8816)      │
│                                                │
│   per inbox message:                           │
│   ┌────────────────────────────────────────┐   │
│   │ docker_runner.run_claude_in_container  │   │
│   │ → spawns `docker run ... image ...`    │   │
│   └────────────────────────────────────────┘   │
│                  ↓ stream-json stdout          │
└─────────────────┬──────────────────────────────┘
                  │
       ┌──────────▼──────────────────────┐
       │  CONTAINER (nexus_agent_isolated:latest)
       │                                  │
       │  claude (npm) + python3 + bridge │
       │                                  │
       │  /etc/agent/mcp_bridge.py        │
       │  /etc/agent/mcp.json             │
       │  /agent/ledger     ← named vol   │
       │  /workspace        ← empty       │
       │                                  │
       │  bridge calls back via           │
       │   http://host.docker.internal:8816│
       └──────────────────────────────────┘
```

The bridge is **baked into the image**, not mounted. The container is
self-contained; only the named ledger volume and a few env vars cross the
boundary.

## Layout

```
nodes/nexus_agent_isolated/
  agent.py            # host process — same shape as nexus_agent.agent
  docker_runner.py    # spawns `docker run`, parses stream-json
  mcp_bridge.py       # MCP stdio server (copied into image)
  mcp.json            # baked-in mcp config pointing claude at the bridge
  Dockerfile          # node:22-bookworm-slim + claude + python3 + bridge
  build_image.sh      # `docker build` helper
  ledger/
    identity.md       # always-loaded into the system prompt
    memory.md         # mutable scratchpad — host-side persistence
    skills/           # markdown skill files
  web/
    server.py         # inspector aiohttp app
    index.html        # vanilla-JS dashboard (re-titled "isolated")
  data/               # logs/ + sessions/ — created on first run
```

## Authentication

`claude` inside the container reads `CLAUDE_CODE_OAUTH_TOKEN` (or
`ANTHROPIC_API_KEY`) from env. On macOS the OAuth token lives in the
keychain, not in `~/.claude/.credentials.json` — so `docker_runner.py`
extracts the token via:

```bash
security find-generic-password -s "Claude Code-credentials" -w
```

and passes it to the container via `-e CLAUDE_CODE_OAUTH_TOKEN=…` at
`docker run` time. (This matches NEXUS's approach in
`api/src/services/oauthSync.ts`.)

If `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` is already set in the
host process's env, that wins. Otherwise the keychain is queried at every
spawn (fast — single shell-out — and always picks up refreshed tokens).

> **Design note:** The original brief suggested mounting `~/.claude:ro`. On
> macOS that directory does **not** contain credentials (they're in the
> keychain), so a mount alone wouldn't authenticate the in-container claude.
> We use the keychain-extraction path instead. The named volume at
> `/agent/ledger` still gives us a stable container-side scratch directory
> that persists across runs.

## Memory persistence

Memory **lives on the host**, in `nodes/nexus_agent_isolated/ledger/memory.md`.
The agent reads/writes via the `memory_read` / `memory_write` MCP tools, which
the bridge translates into HTTP calls back to the host's control server. So
the same file is what the inspector UI shows and what the system prompt
loads at the start of every run.

The named docker volume mounted at `/agent/ledger` is reserved for any
future container-side persistent state (claude session caches, etc.). The
agent has `--tools ""` so it cannot read or write that path directly.

## Surfaces

- `nexus_agent_isolated.inbox` — fire-and-forget — give the agent a task.
- `nexus_agent_isolated.status` — request/response — node, model, image, runs.
- `nexus_agent_isolated.ui_visibility` — request/response — `{action: show|hide}`.

## Mesh tools exposed to the agent (via MCP bridge)

Same as `nexus_agent`:
- `mesh_list_surfaces()`
- `mesh_invoke(target_surface, payload)`
- `mesh_send_to_inbox(target_node, payload)`
- `memory_read()`, `memory_write(content, mode)`
- `list_skills()`, `read_skill(name)`

## Build

```bash
bash nodes/nexus_agent_isolated/build_image.sh
# verify auth wiring (no LLM call, just claude --version):
docker run --rm nexus_agent_isolated:latest --version
```

## Run

```bash
# 1. Source env (for the secret).
source scripts/_env.sh

# 2. Boot the mesh from the demo manifest.
scripts/run_mesh.sh manifests/nexus_agent_isolated_demo.yaml

# 3. Open the inspector.
open http://127.0.0.1:8806

# 4. Send a message via the human dashboard (8802):
#    target = nexus_agent_isolated.inbox
#    payload = {"text": "list mesh surfaces and change webui color to teal"}
```

## Config knobs (env / CLI)

| env                                       | flag                | default                         |
|-------------------------------------------|---------------------|---------------------------------|
| `NEXUS_AGENT_ISOLATED_PORT`               | `--inspector-port`  | `8806`                          |
| `NEXUS_AGENT_ISOLATED_CONTROL_PORT`       | `--control-port`    | `8816`                          |
| `NEXUS_AGENT_ISOLATED_HOST`               | `--inspector-host`  | `127.0.0.1`                     |
| `NEXUS_AGENT_ISOLATED_MODEL`              | `--model`           | `claude-sonnet-4-6`             |
| `NEXUS_AGENT_ISOLATED_IMAGE`              | `--image`           | `nexus_agent_isolated:latest`   |
| `NEXUS_AGENT_ISOLATED_VOLUME`             | `--ledger-volume`   | `nexus_agent_isolated_ledger`   |
| `MESH_CORE_URL`                           | `--core-url`        | `http://127.0.0.1:8000`         |
| `CLAUDE_CODE_OAUTH_TOKEN`                 | (env)               | (keychain fallback on macOS)    |
| `ANTHROPIC_API_KEY`                       | (env)               | (fallback)                      |

> **Why the control server binds to `0.0.0.0`:** the in-container bridge
> reaches the host via `host.docker.internal`, which resolves to the
> docker-host gateway IP, not the host's loopback. Binding the control
> server on 127.0.0.1 wouldn't be reachable from the container. The
> `X-Control-Token` middleware still gates every request.

## Tests

```bash
pytest tests/test_nexus_agent_isolated.py
```

The tests mock `docker run` so they don't require a built image or a live
LLM. A real end-to-end smoke test is the manifest run above.
