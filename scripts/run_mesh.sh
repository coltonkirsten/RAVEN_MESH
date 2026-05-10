#!/usr/bin/env bash
# Run any mesh from a manifest, then print clickable UI links.
#
# Usage:
#   scripts/run_mesh.sh                          # defaults to manifests/full_demo.yaml
#   scripts/run_mesh.sh manifests/demo.yaml
#   scripts/run_mesh.sh path/to/custom.yaml
#   scripts/run_mesh.sh stop                     # stops everything started by this script
#
# What it does:
#   1. Boots Core
#   2. Parses the manifest for node IDs
#   3. Starts each node it knows how to start (looks for scripts/run_<node_id>.sh)
#   4. Skips actor/dummy nodes that are one-shots
#   5. Prints a list of all UI links it detects

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$HERE/_env.sh"

LOG_DIR="$ROOT/.logs"
PID_DIR="$ROOT/.pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

# Map node_id → UI URL. Add new UI-bearing nodes here as they ship.
declare -a UI_NODES=(
  "webui_node|http://localhost:8801|webui"
  "human_node|http://localhost:8802|human dashboard"
  "approval_node|http://localhost:8803|approval queue"
  "nexus_agent|http://localhost:8804|nexus agent inspector"
  "kanban_node|http://localhost:8805|kanban board"
)

start_one() {
  local name="$1"; shift
  local pidfile="$PID_DIR/$name.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "[mesh] $name already running (pid $(cat "$pidfile"))"
    return
  fi
  echo "[mesh] starting $name"
  ( "$@" ) > "$LOG_DIR/$name.log" 2>&1 &
  echo $! > "$pidfile"
}

stop_all() {
  echo "[mesh] stopping all nodes..."
  for pidfile in "$PID_DIR"/*.pid; do
    [[ -f "$pidfile" ]] || continue
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  done
  echo "[mesh] stopped"
}

# Handle stop arg
if [[ "${1:-}" == "stop" ]]; then
  stop_all
  exit 0
fi

MANIFEST="${1:-manifests/full_demo.yaml}"
if [[ ! -f "$MANIFEST" ]]; then
  echo "[mesh] manifest not found: $MANIFEST" >&2
  exit 1
fi

echo "[mesh] using manifest: $MANIFEST"

# Parse node IDs from manifest using Python (handles YAML properly)
NODE_IDS=$(python3 -c "
import sys, yaml
with open('$MANIFEST') as f:
    m = yaml.safe_load(f)
for n in m.get('nodes', []):
    print(n['id'])
")

# Clean slate audit log
rm -f audit.log

# Start Core first
start_one core "$HERE/run_core.sh"
sleep 1

# Iterate manifest nodes, start any with a known run script
STARTED=()
for nid in $NODE_IDS; do
  script="$HERE/run_${nid}.sh"
  if [[ -f "$script" ]]; then
    start_one "$nid" "$script"
    STARTED+=("$nid")
  fi
done

# Give nodes a beat to register
sleep 2

# Print UI links for whatever started + has a UI
echo ""
echo "=========================================="
echo "  Mesh UIs"
echo "=========================================="
ANY_UI=false
for entry in "${UI_NODES[@]}"; do
  IFS='|' read -r nid url label <<< "$entry"
  for s in "${STARTED[@]}"; do
    if [[ "$s" == "$nid" ]]; then
      printf "  %-18s  %s\n" "$label" "$url"
      ANY_UI=true
      break
    fi
  done
done

# Always include the dashboard if available
if [[ -d "$ROOT/dashboard" ]]; then
  printf "  %-18s  %s   (run: cd dashboard && npm run dev)\n" "mesh dashboard" "http://localhost:5180"
  ANY_UI=true
fi

if [[ "$ANY_UI" == "false" ]]; then
  echo "  (no UI-bearing nodes started)"
fi

echo "=========================================="
echo ""
echo "[mesh] running. logs: $LOG_DIR/  |  stop: scripts/run_mesh.sh stop"
echo "[mesh] tail all: tail -f $LOG_DIR/*.log"
