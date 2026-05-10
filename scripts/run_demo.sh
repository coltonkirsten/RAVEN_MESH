#!/usr/bin/env bash
# Boot Core + the protocol-validation demo nodes (tasks, human_approval) and
# tail their logs. The voice_actor is invoked manually as a one-shot.
#
# Usage:
#   scripts/run_demo.sh           # boot Core + tasks + dummy approval, prints urls
#   scripts/run_demo.sh stop      # stops everything
#
# Once running, in another terminal:
#   scripts/run_dummy_actor.sh --node-id voice_actor --target tasks.list --payload '{}'
#   scripts/run_dummy_actor.sh --node-id voice_actor \
#       --target human_approval.inbox \
#       --payload '{"target_surface":"tasks.create","payload":{"title":"hi"}}'

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
# shellcheck disable=SC1091
source "$HERE/_env.sh"

LOG_DIR="${HERE}/../.logs"
PID_DIR="${HERE}/../.pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

start_one() {
  local name="$1"; shift
  local pidfile="$PID_DIR/$name.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "[demo] $name already running (pid $(cat "$pidfile"))"
    return
  fi
  echo "[demo] starting $name -> $LOG_DIR/$name.log"
  ( "$@" ) > "$LOG_DIR/$name.log" 2>&1 &
  echo $! > "$pidfile"
}

stop_all() {
  echo "[demo] stopping..."
  for pidfile in "$PID_DIR"/*.pid; do
    [[ -f "$pidfile" ]] || continue
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  done
  echo "[demo] stopped"
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
    start_one tasks bash -c "source '$HERE/_env.sh' && python3 -m nodes.dummy.dummy_capability --node-id tasks"
    start_one human_approval bash -c "source '$HERE/_env.sh' && python3 -m nodes.dummy.dummy_approval --node-id human_approval"
    sleep 0.5
    cat <<EOF

[demo] booted.
       core         http://${MESH_HOST}:${MESH_PORT}
       audit log    audit.log
       logs in      $LOG_DIR/

  Try:
    scripts/run_dummy_actor.sh --node-id voice_actor --target tasks.list --payload '{}'

  Tail logs:
    tail -f $LOG_DIR/*.log

  Stop:
    scripts/run_demo.sh stop
EOF
    ;;
  *)
    echo "usage: $0 [start|stop]" >&2
    exit 2
    ;;
esac
