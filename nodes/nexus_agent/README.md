# nexus_agent — RAVEN Mesh node

A new node kind: an **actor** that runs Claude Code (the `claude` CLI) as its
agent harness. Receives messages on its inbox, spawns the binary per message
with an MCP bridge wired up, and lets the agent invoke other mesh nodes via
mesh-flavored tools.

This is the structural port of the Nexus "CLI cell" pattern (Chandler/Dev-style
agents) onto the RAVEN Mesh. Intercom is replaced with mesh invocations;
peer discovery is replaced with the mesh's relationship graph.

## Layout

```
nodes/nexus_agent/
  agent.py         # main entry — registers with mesh, owns the inbox handler
  cli_runner.py    # spawns `claude` --output-format stream-json, parses lines
  mcp_bridge.py    # MCP stdio server exposing mesh tools to claude
  ledger/
    identity.md    # always-loaded into the system prompt (seed identity)
    memory.md      # mutable scratchpad — agent reads/writes via memory_* tools
    skills/        # markdown skill files, loaded on demand by read_skill
  web/
    server.py      # inspector UI (aiohttp + SSE)
    index.html     # vanilla-JS dashboard
  data/
    sessions/      # claude session resume state (current.json)
    logs/          # one JSON file per inbox message + each result
```

## Mesh tools exposed to the agent (via MCP bridge)

- `mesh_list_surfaces()` — discover everything I can reach.
- `mesh_invoke(target_surface, payload)` — call a tool surface, await response.
- `mesh_send_to_inbox(target_node, payload)` — fire-and-forget to an inbox.
- `memory_read()`, `memory_write(content, mode)` — persistent ledger.
- `list_skills()`, `read_skill(name)` — load procedural skills on demand.

The bridge is a stdio MCP server (`mcp_bridge.py`) spawned by `claude` via
`--mcp-config`. It calls back into agent.py's loopback "control" HTTP server
(default `127.0.0.1:8814`, X-Control-Token authed) which holds the live
MeshNode SDK instance.

## Surfaces this node exposes

- `nexus_agent.inbox` — `fire_and_forget` — give the agent a task.
- `nexus_agent.status` — `request_response` — node id, model, session, runs.
- `nexus_agent.ui_visibility` — `request_response` — `{action: show|hide}`.

## How to run

```bash
# 1. Boot Core with the nexus_agent demo manifest.
cd /path/to/RAVEN_MESH
MESH_MANIFEST=manifests/nexus_agent_demo.yaml scripts/run_core.sh &

# 2. Boot supporting nodes (any subset of these — agent only needs targets it
#    plans to call).
scripts/run_webui_node.sh &
scripts/run_cron_node.sh &
scripts/run_human_node.sh &
scripts/run_approval_node.sh &

# 3. Boot the agent.
scripts/run_nexus_agent.sh &

# 4. Open the inspector.
open http://127.0.0.1:8804

# 5. Send the agent a message via the human dashboard (8802) — pick
#    nexus_agent.inbox, payload {"text":"call webui_node.show_message with 'hi from agent'"}
#    OR use curl against /v0/invoke directly.
```

## Authentication

`claude` reads the host's macOS keychain OAuth tokens. We pass the parent
environment unchanged into the subprocess. No API key plumbing.

## Config knobs

- `--model` (env `NEXUS_AGENT_MODEL`, default `claude-sonnet-4-6`)
- `--inspector-port` (env `NEXUS_AGENT_PORT`, default `8804`)
- `--control-port` (env `NEXUS_AGENT_CONTROL_PORT`, default `8814`)
- `--core-url` (env `MESH_CORE_URL`, default `http://127.0.0.1:8000`)

## Tests

```bash
pytest tests/test_nexus_agent.py
```

Tests mock the `claude` subprocess — they don't require a real LLM run.
A real end-to-end smoke test takes a live `claude` invocation; see
`scripts/run_nexus_agent.sh` and the manifest above.
