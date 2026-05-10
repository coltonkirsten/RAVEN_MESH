#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
# shellcheck disable=SC1091
source "$HERE/_env.sh"
MANIFEST=${MESH_MANIFEST:-manifests/demo.yaml}
AUDIT_LOG=${AUDIT_LOG:-./audit.log}
exec python3 -m core.core --manifest "$MANIFEST" --host "$MESH_HOST" --port "$MESH_PORT" --audit-log "$AUDIT_LOG"
