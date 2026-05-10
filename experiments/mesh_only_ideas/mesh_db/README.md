# mesh_db_node — the mesh queries itself

A mesh-native capability that exposes Core's `audit.log` as a queryable
database via three typed mesh surfaces.

## Why this is mesh-only

Every routed envelope in RAVEN_MESH lands in `audit.log` with a verified
signature, a `correlation_id`, the `from_node`, the `to_surface`, and a
decision code. That log is **already the system's full state-transition
history** — no per-node observability integration needed.

`mesh_db_node` simply parses `audit.log` and serves it through the same
typed-surface protocol that every other peer speaks. Three primitives compose
into something that is hard to build outside of a mesh:

1. **Audit log** = a single signed timeline of everything that happened.
2. **`correlation_id`** = an automatic chain identifier that crosses nodes /
   runtimes / languages — no SDK plumbing required.
3. **Typed mesh surfaces** = any peer (a voice actor, a kanban node, an LLM
   agent in another runtime) can introspect the system using the same
   protocol it uses for business calls. The introspection itself shows up in
   the log too, so it is recursively observable.

Outside the mesh you would need an OpenTelemetry pipeline plus per-service
correlation-ID glue plus a separate query backend to get even a worse
version of this.

## What it does

| surface | payload | response |
| ------- | ------- | -------- |
| `mesh_db_node.query` | `{where: {from_node?, to_surface?, decision?, type?, correlation_id?, since?}, limit?}` | matching audit entries |
| `mesh_db_node.count` | `{group_by: from_node\|to_surface\|decision\|type}` | `{value: count}` map |
| `mesh_db_node.trace` | `{correlation_id}` | every entry sharing that `correlation_id` (provenance chain) |
| `mesh_db_node.ping`  | anything | echoes — used to populate audit traffic in the demo |

The schemas are at `schemas/mesh_db_query.json`, `schemas/mesh_db_count.json`,
`schemas/mesh_db_trace.json`. Core enforces them at the boundary.

## Run the demo

```bash
bash experiments/mesh_only_ideas/mesh_db/demo.sh
```

In ~10 seconds the script will:

1. Boot Core on port 8044 with `manifests/mesh_db_demo.yaml`.
2. Start `mesh_db_node`.
3. Send 3 `ping` invocations from `demo_actor` → `mesh_db_node.ping`.
4. Ask `mesh_db_node.count` to group `audit.log` by decision and by surface.
5. Ask `mesh_db_node.query` for the last three `mesh_db_node.ping` invocations.
6. Pick one ping's `correlation_id` and ask `mesh_db_node.trace` for the
   full chain — both `invocation` and `response` rows show up, signed and
   correlated.
7. Tear everything down.

## What the demo proves

- A node whose only data is **the audit log of the mesh that hosts it** can
  answer questions about that mesh's behavior in real time, with no extra
  instrumentation in any other node.
- The `correlation_id` chain is end-to-end queryable from inside the mesh.
- Typed surfaces give you a self-documenting introspection API. Anyone with
  the right edge can ask "what happened?" without reading any log file
  themselves.
- The query envelopes themselves get logged, so the introspector is
  observable too.

## Next steps (not built)

- A `mesh_db_node.subscribe` surface that streams new audit entries as they
  happen (hook into Core's `_admin_streams` rather than polling the file).
  Stubbed workaround: today the node re-reads the full file on every call.
  Cheap for v0; a tail+stream pattern would scale.
- Joins across `correlation_id` groups for "mean cost per user request"
  style queries — composes with idea #15 (cost accounting) from the
  brainstorm.
- A small reader for the `wrapped` envelope chain: the audit log records
  decisions but not payload bodies. Pairing with the admin envelope tap
  would give full bodies. Out of scope for the prototype.

## Files

| path | purpose |
| ---- | ------- |
| `mesh_db_node.py` | the node implementation |
| `../../../manifests/mesh_db_demo.yaml` | demo manifest |
| `../../../schemas/mesh_db_query.json` | query input schema |
| `../../../schemas/mesh_db_count.json` | count input schema |
| `../../../schemas/mesh_db_trace.json` | trace input schema |
| `demo.sh` | 30-sec end-to-end demo |
| `../../../tests/test_mesh_db_node.py` | pytest suite (9 tests, in-process) |
