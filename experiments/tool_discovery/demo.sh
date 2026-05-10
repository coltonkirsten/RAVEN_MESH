#!/usr/bin/env bash
# tool_discovery demo — boots Core + composer + kanban with the manifest in
# this directory, calls composer.compose with a natural-language goal, prints
# the resulting tool chain, and shuts everything down.
#
# Requires:  python3, all the project deps (aiohttp, pyyaml, jsonschema, ...)
# Optional:  OPENAI_API_KEY  — without it, composer falls back to a synthetic
#                              regex planner (still demonstrates the pattern).
#
# Usage:
#     bash experiments/tool_discovery/demo.sh
#     bash experiments/tool_discovery/demo.sh "your custom goal here"
#     bash experiments/tool_discovery/demo.sh --keep    # leave processes running
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/scripts/_env.sh"

# Composer's secret isn't in _env.sh (this is a brand-new node), so derive it
# the same way every other node does.
_derive() { printf "mesh:%s:dev" "$1" | shasum -a 256 | cut -d' ' -f1; }
export COMPOSER_AGENT_SECRET=${COMPOSER_AGENT_SECRET:-$(_derive composer_agent)}

KEEP=0
GOAL="create a kanban task to call mom"
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    -h|--help)
      sed -n '2,15p' "$0"
      exit 0
      ;;
    *) GOAL="$arg" ;;
  esac
done

LOG_DIR="$HERE/runs"
mkdir -p "$LOG_DIR"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
RUN_LOG="$LOG_DIR/$RUN_ID"
mkdir -p "$RUN_LOG"

PIDS=()
cleanup() {
  if [[ "$KEEP" -eq 1 ]]; then
    echo
    echo "[demo] --keep set, leaving processes running. PIDs: ${PIDS[*]:-(none)}"
    return
  fi
  echo
  echo "[demo] stopping..."
  for pid in "${PIDS[@]:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

start() {
  local name="$1"; shift
  echo "[demo] starting $name -> $RUN_LOG/$name.log"
  ( "$@" ) > "$RUN_LOG/$name.log" 2>&1 &
  PIDS+=("$!")
}

wait_for_core() {
  for _ in $(seq 1 50); do
    if curl -sf "${MESH_CORE_URL}/v0/healthz" > /dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  echo "[demo] core never came up — see $RUN_LOG/core.log" >&2
  return 1
}

wait_for_node() {
  local nid="$1"
  for _ in $(seq 1 50); do
    if curl -sf -H "X-Admin-Token: ${ADMIN_TOKEN:-admin-dev-token}" \
        "${MESH_CORE_URL}/v0/admin/state" 2>/dev/null \
        | python3 -c "
import json, sys
d = json.load(sys.stdin)
sys.exit(0 if any(c.get('id') == '$nid' and c.get('connected') for c in d.get('nodes', [])) else 1)
" 2>/dev/null; then
      return 0
    fi
    sleep 0.2
  done
  echo "[demo] $nid never connected — see $RUN_LOG/$nid.log" >&2
  return 1
}

# 1. Core with our manifest.
export MESH_MANIFEST="$HERE/manifest.yaml"
export AUDIT_LOG="$RUN_LOG/audit.log"
start core python3 -m core.core \
    --manifest "$MESH_MANIFEST" \
    --host "${MESH_HOST}" --port "${MESH_PORT}" \
    --audit-log "$AUDIT_LOG"
wait_for_core
echo "[demo] core healthy at ${MESH_CORE_URL}"

# 2. Real tool target — kanban_node.
start kanban_node python3 -m nodes.kanban_node.kanban_node \
    --node-id kanban_node
wait_for_node kanban_node
echo "[demo] kanban_node connected"

# 3. The composer itself.
start composer_agent python3 -m experiments.tool_discovery.composer_agent \
    --node-id composer_agent \
    --core-url "${MESH_CORE_URL}" \
    --admin-token "${ADMIN_TOKEN:-admin-dev-token}"
wait_for_node composer_agent
echo "[demo] composer_agent connected"

# Give the composer a beat to finish discovery.
sleep 0.5

# 4. Drive the composer with a natural-language goal via Core's admin/invoke.
echo
echo "[demo] goal: $GOAL"
echo "[demo] invoking composer_agent.compose ..."
RESPONSE_FILE="$RUN_LOG/compose_response.json"
REQUEST_FILE="$RUN_LOG/compose_request.json"
GOAL="$GOAL" python3 - <<'PY' > "$REQUEST_FILE"
import json, os
print(json.dumps({
    "from_node": "human_node",
    "target": "composer_agent.compose",
    "payload": {"goal": os.environ["GOAL"], "max_steps": 4},
}))
PY
HTTP_STATUS=$(curl -sS -o "$RESPONSE_FILE" -w "%{http_code}" \
    -H "X-Admin-Token: ${ADMIN_TOKEN:-admin-dev-token}" \
    -H "Content-Type: application/json" \
    "${MESH_CORE_URL}/v0/admin/invoke" \
    --data-binary "@$REQUEST_FILE")
echo "[demo] HTTP $HTTP_STATUS"
echo
echo "===== compose response ====="
python3 -m json.tool "$RESPONSE_FILE" || cat "$RESPONSE_FILE"
echo "============================"
echo
echo "[demo] response saved to $RESPONSE_FILE"
echo "[demo] composer log:        $RUN_LOG/composer_agent.log"
echo "[demo] kanban log:          $RUN_LOG/kanban_node.log"
echo "[demo] audit log:           $AUDIT_LOG"

# Surface the chain for human readability.
echo
echo "===== tool chain ====="
python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
# admin/invoke returns the full envelope; the composer reply lives in .payload
body = d.get('payload') if isinstance(d, dict) and 'payload' in d else d
for i, step in enumerate(body.get('chain') or []):
    addr = step.get('address')
    args = step.get('arguments')
    res = step.get('result') or step.get('error')
    print(f'  [{i}] {addr}({json.dumps(args)})')
    print(f'       -> {json.dumps(res)[:200]}')
fm = body.get('final_message')
if fm:
    print(f'final: {fm}')
" "$RESPONSE_FILE" || true
echo "======================"

if [[ "$KEEP" -eq 1 ]]; then
  echo
  echo "[demo] processes still running. Stop with: kill ${PIDS[*]}"
  # Don't trap-cleanup.
  trap - EXIT INT TERM
fi
