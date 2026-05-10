# Tool Discovery — schema-driven LLM composition over RAVEN Mesh

## What this is

A working sketch of *runtime tool discovery*: an LLM-driven mesh node that
learns its capabilities by querying Core, not by being hand-coded. Add a
new node to the manifest, give the composer an edge to it, restart — the
new tool appears in the planner's toolkit with zero code changes in the
composer.

## What was built

```
experiments/tool_discovery/
├── mesh_introspect.py     # topology + schema loader (live or manifest)
├── composer_agent.py      # the LLM composer node itself
├── compose.json           # JSON Schema for composer.compose
├── manifest.yaml          # demo manifest: composer + kanban + voice + webui + human
├── demo.sh                # boot → drive → tear down
└── DISCOVERY.md           # this file
```

### `mesh_introspect.py`

Two loaders that produce the same `MeshTopology` dataclass:

- `load_from_manifest(path)` — parses YAML + reads each surface's schema
  file off disk. Works without a running Core; useful for tests/CI.
- `load_from_core(core_url, admin_token)` — hits `GET /v0/admin/state`,
  which embeds full schemas inline. This is what a live agent uses.

Public composer-facing entry point:

```python
discover_capabilities(node_id, core_url, admin_token) -> list[dict]
# each dict: {target_node, surface, address, surface_type,
#             invocation_mode, schema_url, schema_dict}
```

Walks the manifest's `relationships` and returns *only* the surfaces
`node_id` is allowed to invoke — i.e., what the planner can actually
call. Core's edge ACL is the source of truth.

### `composer_agent.py`

A single-purpose actor node:

1. **Boot** — `MeshNode.start()`, then `discover_capabilities(self.node_id,
   ...)`. Caches the result.
2. **Translation** — for each capability, build an OpenAI function tool:

   ```json
   {
     "type": "function",
     "function": {
       "name": "<node>__<surface>",      // dot is illegal in OpenAI names
       "description": "[<address>] <title>: <description>",
       "parameters": <raw JSON Schema, $-keys stripped>
     }
   }
   ```

3. **Surface** — exposes one tool, `compose`, taking
   `{goal, model?, max_steps?, dry_run?}`.
4. **Loop** — chat-completions round-trip. If the model returns
   `tool_calls`, execute each via `MeshNode.invoke(addr, args)`, append the
   result as a `role=tool` message, repeat until the model stops calling
   tools or `max_steps` is hit. Returns the full chain.
5. **Fallback** — if `OPENAI_API_KEY` is unset, a deterministic regex
   planner fires once (kanban-or-voice routing). Demo runs with no key.

OpenAI client is a hand-rolled `aiohttp` POST to `/v1/chat/completions` —
no `openai` package dependency, keeps the surface area tiny.

## Algorithm — execution loop (pseudocode)

```text
def compose(goal):
    capabilities = discover_capabilities(self_id, core_url, admin_token)
    tools = [openai_tool(addr, schema) for (addr, schema) in capabilities]

    messages = [system_prompt, {"role": "user", "content": goal}]
    chain    = []

    for step in range(max_steps):
        msg = openai.chat(model, messages, tools=tools, tool_choice="auto")
        messages.append(assistant(msg))
        if not msg.tool_calls:
            return chain, msg.content                     # done, model summarized

        for tc in msg.tool_calls:
            addr   = decode_tool_name(tc.function.name)   # "k__create_card" -> "k.create_card"
            args   = json.loads(tc.function.arguments)
            try:
                result = mesh.invoke(addr, args)          # real network call to Core
            except MeshError as e:
                result = {"error": e}
            chain.append({addr, args, result})
            messages.append({"role": "tool",
                             "tool_call_id": tc.id,
                             "name": tc.function.name,
                             "content": json.dumps(result)})
    return chain, "(max_steps reached)"
```

Three properties worth naming:

- **The mesh ACL is enforced inside `mesh.invoke`.** The planner can't
  hallucinate a target it doesn't have an edge to — Core rejects the
  envelope. This is the whole reason runtime discovery is safe: the LLM
  *sees* only what it's allowed to call.
- **Schema validation is enforced by Core too.** If gpt-4o-mini emits a
  payload that violates the surface's JSON Schema, Core returns an error
  envelope, the loop feeds that back as a `tool` message, and the model
  retries.
- **The chain is the artifact.** Every step's address, args, and result
  go into `chain[]`. That's the audit trail you replay/inspect.

## Example transcripts

### 1. `goal = "create a kanban task to call mom"` (synthetic planner)

```
[0] kanban_node.create_card({"column": "Backlog", "title": "call mom"})
    -> {"card_id": "card_9f97669d", "card": {... title: "call mom" ...}}
final: (synthetic planner: done)
```

This is the actual run captured in `runs/20260510_010458/`. Note the
synthetic planner correctly extracted "call mom" from the goal text and
chose the default column. With `OPENAI_API_KEY` set, gpt-4o-mini does the
same job with much better natural-language flexibility.

### 2. `goal = "make a card 'review PR' and tell the user out loud"` (gpt-4o-mini, expected)

```
[0] kanban_node.create_card({"column": "Backlog", "title": "review PR"})
    -> {"card_id": "card_b1f8...", ...}
[1] voice_actor.say({"text": "Created card review PR in the backlog."})
    -> {"ok": true}
final: "Done — card created and announced."
```

Demonstrates multi-tool chains across two nodes. Composer wasn't told
about either tool — both were discovered.

### 3. `goal = "what's on the kanban board right now?"` (gpt-4o-mini, expected)

```
[0] kanban_node.get_board({})
    -> {"columns": [...], "cards": [...]}
final: "You have 3 cards: 'call mom' in Backlog, 'review PR' in In Progress, 'ship demo' in Done."
```

Read-only chains work the same way. The model uses the JSON result to
compose a natural-language summary.

## Limitations

- **JSON Schema → OpenAI tool fidelity is approximate.** OpenAI's strict
  mode rejects some draft-07 keywords (`additionalProperties: true`,
  `oneOf` with mixed types). The composer strips `$schema` and passes the
  rest through; in strict-mode setups some surfaces would need a
  schema-rewriter pass.
- **No streaming.** Each tool round is a full chat-completion request.
  For long chains this adds latency; SSE streaming + early-tool-call
  parsing would help.
- **No planning, just reaction.** The composer doesn't think ahead — it
  asks the model one step at a time and feeds back results. For
  multi-step goals where steps depend on prior results this is correct;
  for parallelizable work it's wasteful.
- **Discovery is one-shot.** Capabilities are cached at boot. If the
  manifest is hot-reloaded, the composer needs a restart (or a
  `rediscover` surface — a five-line addition).
- **Synthetic fallback is regex-routed.** Without `OPENAI_API_KEY` the
  fallback only handles kanban + voice keywords. It exists so the demo
  runs offline; it isn't the contribution.
- **No approval gating in this manifest.** A real deployment would route
  destructive composer calls through `approval_node` first — the edge
  syntax is identical, just `composer_agent → approval_node.inbox →
  kanban_node.delete_card`. The composer code doesn't change.
- **Tool name encoding is lossy.** `node.surface` → `node__surface` is
  fine for current node IDs (alphanumeric + underscore), but a future
  node ID containing `__` would round-trip wrong. A reversible escape
  would fix this.

## Why this matters

The mesh already has typed surfaces and per-edge ACLs. What was missing
was: an agent doesn't know what tools it has until someone hard-codes
them. This experiment closes that gap. The composer is generic — point
it at any manifest and it will try to drive whatever it's allowed to
drive. New nodes plug in without touching the agent code. That's the
"learn capabilities at runtime" property.

## Run it

```bash
bash experiments/tool_discovery/demo.sh
# or with a custom goal:
bash experiments/tool_discovery/demo.sh "list everything on the kanban board"
# leave processes up for poking:
bash experiments/tool_discovery/demo.sh --keep
```

Set `OPENAI_API_KEY` in your environment to use gpt-4o-mini; without it
the synthetic planner runs and proves the plumbing works.
