#!/usr/bin/env bash
# 30-second demo: boot Core + mesh_db_node, fire ping traffic, query the
# audit log via the mesh itself, print results, then clean up.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source scripts/_env.sh

# Per-experiment secrets (deterministic per node id).
_derive() { printf "mesh:%s:dev" "$1" | shasum -a 256 | cut -d' ' -f1; }
export MESH_DB_NODE_SECRET=${MESH_DB_NODE_SECRET:-$(_derive mesh_db_node)}
export DEMO_ACTOR_SECRET=${DEMO_ACTOR_SECRET:-$(_derive demo_actor)}

PORT=${MESH_DB_DEMO_PORT:-8044}
LOGDIR="$HERE/.logs"
PIDDIR="$HERE/.pids"
AUDIT="$HERE/audit.log"
mkdir -p "$LOGDIR" "$PIDDIR"

cleanup() {
  echo "[mesh_db demo] shutting down..."
  for pf in "$PIDDIR"/*.pid; do
    [[ -f "$pf" ]] || continue
    pid=$(cat "$pf")
    if kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null || true; fi
    rm -f "$pf"
  done
}
trap cleanup EXIT

rm -f "$AUDIT"

echo "[mesh_db demo] booting Core on :$PORT (manifest=mesh_db_demo.yaml)..."
( AUDIT_LOG="$AUDIT" \
  python3 -m core.core \
    --manifest manifests/mesh_db_demo.yaml \
    --host 127.0.0.1 --port "$PORT" --audit-log "$AUDIT" \
) > "$LOGDIR/core.log" 2>&1 &
echo $! > "$PIDDIR/core.pid"
sleep 1

echo "[mesh_db demo] booting mesh_db_node..."
( MESH_CORE_URL="http://127.0.0.1:$PORT" AUDIT_LOG="$AUDIT" \
  python3 -m experiments.mesh_only_ideas.mesh_db.mesh_db_node \
) > "$LOGDIR/mesh_db.log" 2>&1 &
echo $! > "$PIDDIR/mesh_db.pid"
sleep 1

run_actor() {
  MESH_CORE_URL="http://127.0.0.1:$PORT" \
  DEMO_ACTOR_SECRET="$DEMO_ACTOR_SECRET" \
  python3 -m nodes.dummy.dummy_actor \
    --node-id demo_actor --target "$1" --payload "$2"
}

echo
echo "[mesh_db demo] firing 3 ping invocations to mesh_db_node.ping..."
for i in 1 2 3; do
  run_actor mesh_db_node.ping "{\"i\": $i}" >/dev/null
done

echo
echo "[mesh_db demo] === count audit entries grouped by decision ==="
run_actor mesh_db_node.count '{"group_by":"decision"}'

echo
echo "[mesh_db demo] === count audit entries grouped by to_surface ==="
run_actor mesh_db_node.count '{"group_by":"to_surface"}'

echo
echo "[mesh_db demo] === query last 3 invocations to mesh_db_node.ping ==="
run_actor mesh_db_node.query '{"where":{"to_surface":"mesh_db_node.ping","type":"invocation"},"limit":3}'

echo
echo "[mesh_db demo] === trace one ping correlation chain ==="
# Pick one correlation_id from audit.log directly.
CID=$(python3 -c "
import json, sys
for line in open('$AUDIT'):
    e = json.loads(line)
    if e.get('to_surface') == 'mesh_db_node.ping' and e.get('type') == 'invocation':
        print(e['correlation_id']); sys.exit(0)
")
echo "[mesh_db demo] tracing correlation_id=$CID"
run_actor mesh_db_node.trace "{\"correlation_id\":\"$CID\"}"

echo
echo "[mesh_db demo] DONE. core log: $LOGDIR/core.log  audit: $AUDIT"
