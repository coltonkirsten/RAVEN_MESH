#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
# shellcheck disable=SC1091
source "$HERE/_env.sh"
exec python3 -m nodes.human_node.human_node --node-id human_node "$@"
