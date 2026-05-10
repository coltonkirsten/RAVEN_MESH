# mesh_only_top1 — `mesh_chronicle`

Causal-chain time-travel debugger built on top of RAVEN_MESH's structured
surfaces, signed envelopes, and admin observability — no protocol changes.

## What's here

```
mesh_chronicle/         the new node + recorder + replayer + differ
manifests/              v1 (loose) and v2 (strict) demo manifests
schemas/                surface schemas for chronicle, echo, client
web/index.html          single-page inspector UI
scripts/demo.sh         end-to-end demo
tests/test_chronicle.py 7 integration tests, all passing
DEMO.md                 1000-word walkthrough with real I/O
PROTOCOL_TAGS.md        per-file layer audit
```

## Run the demo

```
PYTHONPATH=$(pwd):$(pwd)/experiments/mesh_only_top1 \
  bash experiments/mesh_only_top1/scripts/demo.sh
```

The inspector is at <http://127.0.0.1:9100/inspector>.

## Run the tests

```
PYTHONPATH=$(pwd):$(pwd)/experiments/mesh_only_top1 \
  pytest experiments/mesh_only_top1/tests -v
```

## Mesh surfaces exposed

```
chronicle.list_chains    — recent captured causal chains, with filters
chronicle.get_chain      — full envelope tree for a correlation_id
chronicle.replay         — re-invoke one captured invocation
chronicle.replay_chain   — re-invoke every invocation in the chain
chronicle.replay_diff    — replay + diff against original response
chronicle.schema_compat  — which captured payloads now fail current schemas
chronicle.reverify       — recompute HMAC over captured envelopes
```

See `DEMO.md` for the why and `PROTOCOL_TAGS.md` for the layer audit.
