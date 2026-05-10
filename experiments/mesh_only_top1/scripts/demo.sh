#!/usr/bin/env bash
# End-to-end demo for mesh_only_top1 (mesh_chronicle).
#
# Boots Core with the v1 manifest (loose echo schema), starts the echo
# capability + chronicle, fires a burst of pings, then hot-reloads the v2
# manifest (strict schema) and asks chronicle.schema_compat which old
# invocations would now fail.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
EXP="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$EXP/../.." && pwd)"
# shellcheck disable=SC1091
source "$HERE/_env.sh"

cd "$REPO"

LOGDIR="$EXP/.logs"
mkdir -p "$LOGDIR"
rm -f "$EXP/.chronicle/recordings.jsonl" || true

cleanup() {
  set +e
  for pid in $CORE_PID $ECHO_PID $CHRON_PID; do
    [[ -n "${pid:-}" ]] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT

echo "==> [1/6] starting Core with v1 manifest (loose echo schema)"
PYTHONPATH="$REPO:$EXP" python3 -m core.core \
  --manifest "$EXP/manifests/chronicle_demo_v1.yaml" \
  --host "$MESH_HOST" --port "$MESH_PORT" \
  --audit-log "$LOGDIR/audit.log" \
  > "$LOGDIR/core.log" 2>&1 &
CORE_PID=$!

# wait for core
for _ in $(seq 1 50); do
  if curl -fsS "$MESH_CORE_URL/v0/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

echo "==> [2/6] starting echo_capability"
PYTHONPATH="$REPO:$EXP" python3 -m mesh_chronicle.echo_capability \
  --core-url "$MESH_CORE_URL" \
  > "$LOGDIR/echo.log" 2>&1 &
ECHO_PID=$!

echo "==> [3/6] starting mesh_chronicle (inspector at http://127.0.0.1:${CHRONICLE_INSPECTOR_PORT}/inspector)"
CHRONICLE_STORE="$EXP/.chronicle/recordings.jsonl" \
PYTHONPATH="$REPO:$EXP" python3 -m mesh_chronicle.chronicle_node \
  --core-url "$MESH_CORE_URL" \
  > "$LOGDIR/chronicle.log" 2>&1 &
CHRON_PID=$!

# Wait for all three to register.
sleep 1.5

echo "==> [4/6] driving 5 pings as client_actor"
PYTHONPATH="$REPO:$EXP" python3 -m mesh_chronicle.demo_client \
  --core-url "$MESH_CORE_URL" --count 5

# Let the chronicle drain the SSE tap before we hot-reload.
sleep 0.5

echo "==> [5/6] hot-reloading manifest -> v2 (strict echo schema requires user_id pattern u_*)"
cp "$EXP/manifests/chronicle_demo_v1.yaml" "$EXP/manifests/chronicle_demo_v1.yaml.bak"
cat "$EXP/manifests/chronicle_demo_v2.yaml" \
  | curl -fsS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
      -H "Content-Type: application/x-yaml" \
      --data-binary @- \
      "$MESH_CORE_URL/v0/admin/manifest" \
  | python3 -m json.tool

echo "==> [6/6] running chronicle.schema_compat (regression detection)"
PYTHONPATH="$REPO:$EXP" python3 - <<'PY'
import asyncio, json, os
from node_sdk import MeshNode
async def main():
    node = MeshNode("client_actor",
                    os.environ["CLIENT_ACTOR_SECRET"],
                    os.environ["MESH_CORE_URL"])
    await node.connect(); await node.serve()
    out = await node.invoke("mesh_chronicle.schema_compat", {"limit": 200})
    print(json.dumps(out, indent=2))
    await node.stop()
asyncio.run(main())
PY

echo
echo "demo done. logs in $LOGDIR/."
echo "inspector still running at http://127.0.0.1:${CHRONICLE_INSPECTOR_PORT}/inspector — press ctrl-C to stop everything."

# Restore v1 manifest so reruns are clean.
mv "$EXP/manifests/chronicle_demo_v1.yaml.bak" "$EXP/manifests/chronicle_demo_v1.yaml" 2>/dev/null || true

wait
