# mesh_chronicle — causal-chain time-travel debugger

A working prototype that captures every envelope flowing through Core, indexes
them by `correlation_id` into causal chains, and lets you (a) replay any chain
against the live mesh, (b) diff replayed responses against original ones, and
(c) detect schema-compatibility regressions when a manifest revision tightens
a surface schema.

This document walks through the demo end-to-end with real input and output.

## Why this is mesh-only

Four properties of the RAVEN_MESH protocol — none of which are properties of a
generic microservice mesh — make this prototype possible without modifying any
participating node:

1. **Every envelope is HMAC-signed.** A captured recording is tamper-evident:
   `chronicle.reverify` recomputes HMACs over each captured envelope using
   the manifest's identity secrets, so a forged or mutated recording is
   detectable cryptographically.
2. **Every envelope carries a `correlation_id`.** The full causal DAG of a
   multi-hop interaction (A → B, B → C, C responds, B responds, A returns) is
   reconstructible from a flat capture with zero per-node instrumentation.
3. **Every surface has a JSON Schema in the manifest.** When the manifest is
   revised — for example, `echo_v1.json` (loose, optional fields) becomes
   `echo_v2.json` (strict, required fields, additional-properties false) —
   chronicle can validate every captured payload against the *current*
   schema and report exactly which historical invocations would now break.
4. **Every envelope flows through Core.** The existing `/v0/admin/stream`
   SSE tap gives an observer perfect visibility, so the recorder needs no
   instrumentation in the participating nodes.

In a generic microservice setup, achieving the equivalent requires distributed
tracing (Jaeger/Zipkin), an external schema registry (Confluent Schema Registry
or similar), per-message cryptographic attestation, and per-service observation
hooks. Mesh has all four properties as protocol-level guarantees.

## Layer ownership

The protocol is **untouched**. Every line of new code lives in the opinionated
layer under `experiments/mesh_only_top1/`. The replayer uses the existing
`/v0/admin/invoke` endpoint (a v0 protocol surface) to synthesize envelopes
from the original sender's identity. Core already records `admin_synthesized:
true` on those envelopes, so replays are visibly distinct from organic traffic.
See `PROTOCOL_TAGS.md` for the full per-line layer audit.

## Running the demo

```
$ cd RAVEN_MESH
$ PYTHONPATH=$(pwd):$(pwd)/experiments/mesh_only_top1 \
    bash experiments/mesh_only_top1/scripts/demo.sh
```

The script boots Core, the echo capability, and `mesh_chronicle`, then drives
five pings as `client_actor`, hot-swaps the manifest from `echo_v1` (loose) to
`echo_v2` (strict), and asks `chronicle.schema_compat` to report regressions.

## Step-by-step output

### 1. Boot

```
==> [1/6] starting Core with v1 manifest (loose echo schema)
==> [2/6] starting echo_capability
==> [3/6] starting mesh_chronicle (inspector at http://127.0.0.1:9100/inspector)
```

Core, echo, and chronicle each register over the wire. The chronicle
subscribes to `/v0/admin/stream` and begins capturing.

### 2. Drive five pings — alternating "legacy" and "new" payloads

```
==> [4/6] driving 5 pings as client_actor
[client_actor] sending 5 pings to echo_capability.ping ...
  ping#0 payload={'text': 'legacy ping #0', 'session': 'abc'} -> call_index=1
  ping#1 payload={'text': 'new ping #1', 'user_id': 'u_demo1'} -> call_index=2
  ping#2 payload={'text': 'legacy ping #2', 'session': 'abc'} -> call_index=3
  ping#3 payload={'text': 'new ping #3', 'user_id': 'u_demo3'} -> call_index=4
  ping#4 payload={'text': 'legacy ping #4', 'session': 'abc'} -> call_index=5
```

Three of these payloads carry `session` (legacy clients, no `user_id`); two
carry `user_id` matching `^u_[A-Za-z0-9]+$`. Under the v1 schema, all five
pass — `additionalProperties: true`, no required fields beyond `text`.

### 3. List captured chains, through the mesh

The client invokes `mesh_chronicle.list_chains` like any other surface:

```json
{
  "from": "mesh_chronicle",
  "kind": "response",
  "payload": {
    "chains": [
      {"correlation_id": "b43aa195-…", "root_from": "client_actor",
       "root_to": "echo_capability.ping", "envelope_count": 2,
       "terminal_status": "ok"},
      {"correlation_id": "b119905b-…", "root_from": "client_actor",
       "root_to": "echo_capability.ping", "envelope_count": 2,
       "terminal_status": "ok"},
      …
    ],
    "total_known": 5
  }
}
```

Each chain is the invocation envelope plus its response, grouped by
`correlation_id`. No participating node had to be modified; the chronicle
sees them via the admin tap.

### 4. Hot-swap the manifest to v2

```
==> [5/6] hot-reloading manifest -> v2 (strict echo schema requires user_id)
{"ok": true, "manifest_path": "…/chronicle_demo_v1.yaml",
 "nodes_declared": 3, "edges": 8}
```

Core re-reads its manifest. `echo_capability.ping` now points at
`echo_v2.json`, which has `additionalProperties: false`, `required: [text,
user_id]`, and `user_id` matches `^u_[A-Za-z0-9]+$`.

### 5. Run schema-compat regression detection

The client invokes `mesh_chronicle.schema_compat`:

```json
{
  "payload": {
    "total_invocations_checked": 7,
    "now_breaking": 3,
    "report": [
      {"correlation_id": "b43aa195-…", "checks": [{
        "target": "echo_capability.ping",
        "compatible": false,
        "reason": "schema_violation",
        "details": "Additional properties are not allowed ('session' was unexpected) …"
      }]},
      {"correlation_id": "b119905b-…", "checks": [{
        "target": "echo_capability.ping", "compatible": true
      }]},
      …
    ]
  }
}
```

Three legacy invocations are flagged as **incompatible** under the new schema
— the same three that carry `session: "abc"` and lack `user_id`. The two
`u_demo*` payloads remain compatible. The chronicle has, in effect, run a
production-traffic compatibility test against a proposed schema change
without replaying anything yet — pure observation against the existing
manifest revision.

### 6. Inspector

The chronicle ships a single-page web inspector at
`http://127.0.0.1:9100/inspector`. The left pane lists captured chains; the
right pane shows the envelope tree. Buttons: `Replay`, `Replay + diff`,
`Re-verify HMAC`, and `Run schema-compat regression check`. Each button calls
the corresponding chronicle surface over the mesh (the inspector itself is a
sibling-process tool that talks plain HTTP to the chronicle's surfaces; the
mesh remains the system of record).

## What the replay paths look like

`chronicle.replay` re-issues the captured invocation through `/v0/admin/invoke`:

```
{"captured_msg_id": "fb…", "from_node": "client_actor",
 "target": "echo_capability.ping", "status": 200,
 "response": {"kind": "response",
              "payload": {"echoed": {"text": "ping #0", …}, "call_index": 7}}}
```

`chronicle.replay_diff` compares old vs new responses field-by-field. In this
demo `call_index` increments on every echo call, so the diff shows
`$.call_index — value_changed: 1 → 7` while `$.echoed.text` is unchanged —
exactly the kind of drift you'd want to surface in a regression suite.

## Tests

Seven tests, all passing:

```
$ PYTHONPATH=$(pwd):$(pwd)/experiments/mesh_only_top1 \
    pytest experiments/mesh_only_top1/tests -v
…
test_recorder_captures_envelopes              PASSED
test_chronicle_list_chains_via_mesh           PASSED
test_replay_reproduces_invocation             PASSED
test_replay_diff_detects_state_drift          PASSED
test_schema_compat_after_manifest_v2          PASSED
test_reverify_uses_node_secrets               PASSED
test_payload_differ_unit                      PASSED
```

Each one boots Core in-process and exercises the chronicle through the same
mesh-invocation path the inspector and demo use. No mocks of the protocol.
