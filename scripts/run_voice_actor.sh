#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
# shellcheck disable=SC1091
source "$HERE/_env.sh"
# OPENAI_API_KEY is passed through the environment if set.
exec python3 -m nodes.voice_actor.voice_actor --node-id voice_actor "$@"
