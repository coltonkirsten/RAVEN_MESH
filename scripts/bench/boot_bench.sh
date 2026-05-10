#!/usr/bin/env bash
# Cold boot timing: spawn Core + 1 echo node + 1 client, measure
# (a) wall time from `python -m core.core` spawn to first /v0/healthz 200,
# (b) total time to first successful round-trip invocation.
#
# Reads /tmp/raven_bench_env.sh for secrets/admin token (see python_bench.py).

set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../.."
# shellcheck disable=SC1091
source /tmp/raven_bench_env.sh

ITERS="${1:-10}"
RESULTS_FILE="${2:-/tmp/bench_boot.txt}"
: > "$RESULTS_FILE"

python_bin="$(command -v python3)"

for i in $(seq 1 "$ITERS"); do
  # ensure port is free
  while lsof -ti :8000 >/dev/null 2>&1; do
    lsof -ti :8000 | xargs kill -9 2>/dev/null || true
    sleep 0.2
  done
  rm -f .logs/bench/boot_core.log .logs/bench/boot_echo.log audit_bench.log

  t_start=$($python_bin -c 'import time; print(time.time())')

  ADMIN_TOKEN="$ADMIN_TOKEN" \
  AUDIT_LOG=audit_bench.log \
  MESH_ADMIN_RATE_LIMIT=0 \
  BENCH_CLIENT_SECRET="$BENCH_CLIENT_SECRET" \
  BENCH_ECHO_SECRET="$BENCH_ECHO_SECRET" \
  $python_bin -m core.core --manifest manifests/bench.yaml --port 8000 \
    > .logs/bench/boot_core.log 2>&1 &
  CPID=$!

  # wait for /v0/healthz to be 200
  while true; do
    if curl -sf -m 0.2 http://127.0.0.1:8000/v0/healthz > /dev/null 2>&1; then
      t_health=$($python_bin -c 'import time; print(time.time())')
      break
    fi
    sleep 0.02
  done

  # spawn echo node
  BENCH_ECHO_SECRET="$BENCH_ECHO_SECRET" \
  MESH_CORE_URL="$MESH_CORE_URL" \
  $python_bin -m nodes.dummy.dummy_capability --node-id bench_echo \
    > .logs/bench/boot_echo.log 2>&1 &
  EPID=$!

  # poll healthz until both nodes connected (=2)
  while true; do
    out=$(curl -s -m 0.2 http://127.0.0.1:8000/v0/healthz || echo '')
    if echo "$out" | grep -q '"nodes_connected": 1'; then
      t_connected=$($python_bin -c 'import time; print(time.time())')
      break
    fi
    sleep 0.02
  done

  # do a single round-trip from a one-shot dummy_actor
  BENCH_CLIENT_SECRET="$BENCH_CLIENT_SECRET" \
  MESH_CORE_URL="$MESH_CORE_URL" \
  $python_bin -m nodes.dummy.dummy_actor --node-id bench_client --target bench_echo.ping --payload '{}' \
    > /dev/null 2>&1
  t_first_invoke=$($python_bin -c 'import time; print(time.time())')

  # cleanup
  kill $EPID $CPID 2>/dev/null || true
  wait $EPID 2>/dev/null || true
  wait $CPID 2>/dev/null || true

  $python_bin - <<EOF >> "$RESULTS_FILE"
import json
print(json.dumps({
  "iter": $i,
  "t_health_s": round($t_health - $t_start, 4),
  "t_node_connected_s": round($t_connected - $t_start, 4),
  "t_first_invoke_s": round($t_first_invoke - $t_start, 4),
}))
EOF
done

cat "$RESULTS_FILE"
