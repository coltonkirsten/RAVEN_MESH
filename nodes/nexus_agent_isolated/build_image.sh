#!/usr/bin/env bash
# Build the nexus_agent_isolated docker image.
#
# The image bakes in claude (npm) + python3 + the MCP bridge. Auth is NOT
# baked in — the host extracts the OAuth token from the macOS keychain and
# passes it via env at `docker run` time.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TAG="${NEXUS_AGENT_ISOLATED_IMAGE:-nexus_agent_isolated:latest}"

cd "$HERE"
echo "[build] building $TAG from $HERE"
docker build --tag "$TAG" .
echo "[build] done. image: $TAG"
docker images "$TAG" --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}'
