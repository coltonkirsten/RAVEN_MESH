#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
# shellcheck disable=SC1091
source "$HERE/_env.sh"

if [[ -z "${NEXUS_AGENT_ISOLATED_SECRET:-}" ]]; then
  export NEXUS_AGENT_ISOLATED_SECRET=$(printf "mesh:%s:dev" "nexus_agent_isolated" | shasum -a 256 | cut -d' ' -f1)
fi

exec python3 -m nodes.nexus_agent_isolated.agent --node-id nexus_agent_isolated "$@"
