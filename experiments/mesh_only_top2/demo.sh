#!/usr/bin/env bash
# Provenance-replay demo. Boots Core + counter_node + replay_node, drives some
# traffic to the counter, then proves time-travel by:
#   (a) listing the captured chains,
#   (b) inspecting one chain envelope-by-envelope,
#   (c) resetting counter_node to zero,
#   (d) asking replay_node to re-fire that chain via /v0/admin/invoke,
#   (e) reading counter_node.get and showing the value matches the original,
#   (f) running the chain again with a 'mutate' that doubles every increment,
#   (g) diffing the two replays.
#
# Layer: opinionated. Nothing here changes the protocol.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source scripts/_env.sh

# Per-experiment secrets (deterministic per node id; same trick as scripts/_env.sh).
_derive() { printf "mesh:%s:dev" "$1" | shasum -a 256 | cut -d' ' -f1; }
export REPLAY_NODE_SECRET=${REPLAY_NODE_SECRET:-$(_derive replay_node)}
export COUNTER_NODE_SECRET=${COUNTER_NODE_SECRET:-$(_derive counter_node)}
export DEMO_ACTOR_SECRET=${DEMO_ACTOR_SECRET:-$(_derive demo_actor)}

# replay_node taps /v0/admin/stream so it needs an admin token. Generate a
# strong throwaway one for the demo run; do NOT use the legacy default.
export ADMIN_TOKEN=${ADMIN_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_hex(16))')}

PORT=${REPLAY_DEMO_PORT:-8048}
LOGDIR="$HERE/.logs"
PIDDIR="$HERE/.pids"
AUDIT="$HERE/audit.log"
CAPTURE="$HERE/replay_node/captures.jsonl"
mkdir -p "$LOGDIR" "$PIDDIR"

cleanup() {
  echo
  echo "[replay demo] shutting down..."
  for pf in "$PIDDIR"/*.pid; do
    [[ -f "$pf" ]] || continue
    pid=$(cat "$pf")
    if kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null || true; fi
    rm -f "$pf"
  done
}
trap cleanup EXIT

rm -f "$AUDIT" "$CAPTURE"

echo "[replay demo] booting Core on :$PORT (manifest=replay_demo.yaml)..."
( AUDIT_LOG="$AUDIT" \
  python3 -m core.core \
    --manifest experiments/mesh_only_top2/manifests/replay_demo.yaml \
    --host 127.0.0.1 --port "$PORT" --audit-log "$AUDIT" \
) > "$LOGDIR/core.log" 2>&1 &
echo $! > "$PIDDIR/core.pid"
sleep 1.5

echo "[replay demo] booting counter_node..."
( MESH_CORE_URL="http://127.0.0.1:$PORT" \
  python3 -m experiments.mesh_only_top2.counter_node.counter_node \
) > "$LOGDIR/counter.log" 2>&1 &
echo $! > "$PIDDIR/counter.pid"
sleep 0.8

echo "[replay demo] booting replay_node..."
( MESH_CORE_URL="http://127.0.0.1:$PORT" REPLAY_CAPTURE="$CAPTURE" \
  python3 -m experiments.mesh_only_top2.replay_node.replay_node \
) > "$LOGDIR/replay.log" 2>&1 &
echo $! > "$PIDDIR/replay.pid"
sleep 1.0

run_actor() {
  MESH_CORE_URL="http://127.0.0.1:$PORT" \
  DEMO_ACTOR_SECRET="$DEMO_ACTOR_SECRET" \
  python3 -m nodes.dummy.dummy_actor \
    --node-id demo_actor --target "$1" --payload "$2"
}

echo
echo "[replay demo] === drive original traffic: 3 increments through counter_node ==="
run_actor counter_node.reset '{}' >/dev/null
run_actor counter_node.increment '{"by": 1}' >/dev/null
run_actor counter_node.increment '{"by": 2}' >/dev/null
run_actor counter_node.increment '{"by": 4}' >/dev/null
ORIG_VALUE=$(run_actor counter_node.get '{}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["payload"]["value"])')
echo "[replay demo] counter is now: $ORIG_VALUE   (expected 7)"

# Give replay_node a moment to drain the admin/stream tap.
sleep 0.5

echo
echo "[replay demo] === replay_node.list (chains it has captured so far) ==="
run_actor replay_node.list '{"to_surface":"counter_node.increment"}'

# Pick the correlation_id of the last increment chain.
INCREMENT_CID=$(run_actor replay_node.list '{"to_surface":"counter_node.increment","limit":1}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin)["payload"];print(d["chains"][-1]["correlation_id"])')
echo
echo "[replay demo] picked one chain to inspect: $INCREMENT_CID"

echo
echo "[replay demo] === replay_node.chain (full envelope chain for that id) ==="
run_actor replay_node.chain "{\"correlation_id\":\"$INCREMENT_CID\"}"

echo
echo "[replay demo] === reset counter and replay all 3 increments via /v0/admin/invoke ==="
run_actor counter_node.reset '{}' >/dev/null

# Replay each of the 3 increment chains we captured.
INCS=$(run_actor replay_node.list '{"to_surface":"counter_node.increment"}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin)["payload"];[print(c["correlation_id"]) for c in d["chains"]]')
for cid in $INCS; do
  run_actor replay_node.run "{\"correlation_id\":\"$cid\"}" >/dev/null
done

REPLAY_VALUE=$(run_actor counter_node.get '{}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["payload"]["value"])')
echo "[replay demo] counter after replay: $REPLAY_VALUE   (expected $ORIG_VALUE)"

echo
echo "[replay demo] === A/B mutation: replay each increment with by=10 ==="
run_actor counter_node.reset '{}' >/dev/null
for cid in $INCS; do
  run_actor replay_node.run \
    "{\"correlation_id\":\"$cid\",\"mutate\":{\"to_surface\":\"counter_node.increment\",\"set\":{\"by\":10}}}" \
    >/dev/null
done
MUT_VALUE=$(run_actor counter_node.get '{}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["payload"]["value"])')
echo "[replay demo] counter after mutated replay: $MUT_VALUE   (expected 30)"

echo
echo "[replay demo] === replay_node.diff: original vs mutated chain (first chain only) ==="
FIRST_CID=$(echo "$INCS" | head -n 1)
# Re-run cleanly so we have isolated replay correlation_ids to diff.
ORIG_REPLAY_CID=$(run_actor replay_node.run "{\"correlation_id\":\"$FIRST_CID\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["payload"]["replay_correlation_id"])')
MUT_REPLAY_CID=$(run_actor replay_node.run \
  "{\"correlation_id\":\"$FIRST_CID\",\"mutate\":{\"to_surface\":\"counter_node.increment\",\"set\":{\"by\":99}}}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["payload"]["replay_correlation_id"])')
sleep 0.3
run_actor replay_node.diff "{\"left_correlation_id\":\"$ORIG_REPLAY_CID\",\"right_correlation_id\":\"$MUT_REPLAY_CID\"}"

echo
echo "[replay demo] DONE."
echo "  core log:    $LOGDIR/core.log"
echo "  replay log:  $LOGDIR/replay.log"
echo "  counter log: $LOGDIR/counter.log"
echo "  audit log:   $AUDIT"
echo "  captures:    $CAPTURE"
