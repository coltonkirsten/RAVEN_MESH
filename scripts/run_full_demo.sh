#!/usr/bin/env bash
# Boot Core with the full_demo manifest plus all four real nodes. Useful for
# the manual sanity demo (open the three dashboards, send messages between them).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
# shellcheck disable=SC1091
source "$HERE/_env.sh"

export MESH_MANIFEST=manifests/full_demo.yaml

LOG_DIR="${HERE}/../.logs"
PID_DIR="${HERE}/../.pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

start_one() {
  local name="$1"; shift
  local pidfile="$PID_DIR/$name.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "[full] $name already running (pid $(cat "$pidfile"))"
    return
  fi
  echo "[full] starting $name -> $LOG_DIR/$name.log"
  ( "$@" ) > "$LOG_DIR/$name.log" 2>&1 &
  echo $! > "$pidfile"
}

stop_all() {
  echo "[full] stopping..."
  for pidfile in "$PID_DIR"/*.pid; do
    [[ -f "$pidfile" ]] || continue
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  done
  echo "[full] stopped"
}

case "${1:-start}" in
  stop)
    stop_all
    exit 0
    ;;
  start)
    rm -f audit.log
    start_one core "$HERE/run_core.sh"
    sleep 1
    start_one cron_node "$HERE/run_cron_node.sh"
    start_one webui_node "$HERE/run_webui_node.sh"
    start_one human_node "$HERE/run_human_node.sh"
    start_one approval_node "$HERE/run_approval_node.sh"
    sleep 0.7
    cat <<EOF

[full] booted with manifest=$MESH_MANIFEST
   core            http://${MESH_HOST}:${MESH_PORT}
   webui_node      http://127.0.0.1:8801
   human_node      http://127.0.0.1:8802
   approval_node   http://127.0.0.1:8803
   audit log       audit.log
   logs            $LOG_DIR/

  Try:  open http://127.0.0.1:8801 (webui) AND http://127.0.0.1:8802 (human)
        then in human_node dashboard, pick webui_node.show_message and send
        {"text": "hello from human"}.

  Stop: scripts/run_full_demo.sh stop
EOF
    ;;
  *)
    echo "usage: $0 [start|stop]" >&2
    exit 2
    ;;
esac
