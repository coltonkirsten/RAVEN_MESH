#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
# shellcheck disable=SC1091
source "$HERE/_env.sh"

# Derive a stable per-node secret if not already exported.
if [[ -z "${NEXUS_AGENT_SECRET:-}" ]]; then
  export NEXUS_AGENT_SECRET=$(printf "mesh:%s:dev" "nexus_agent" | shasum -a 256 | cut -d' ' -f1)
fi

exec python3 -m nodes.nexus_agent.agent --node-id nexus_agent "$@"
