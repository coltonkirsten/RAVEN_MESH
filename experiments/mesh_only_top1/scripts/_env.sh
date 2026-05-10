#!/usr/bin/env bash
# Sourced by demo + run scripts. Mirrors the upstream scripts/_env.sh secret
# derivation so manifests using identity_secret: env:FOO_SECRET resolve.

_derive() { printf "mesh:%s:dev" "$1" | shasum -a 256 | cut -d' ' -f1; }

export CLIENT_ACTOR_SECRET=${CLIENT_ACTOR_SECRET:-$(_derive client_actor)}
export ECHO_CAPABILITY_SECRET=${ECHO_CAPABILITY_SECRET:-$(_derive echo_capability)}
export MESH_CHRONICLE_SECRET=${MESH_CHRONICLE_SECRET:-$(_derive mesh_chronicle)}

export ADMIN_TOKEN=${ADMIN_TOKEN:-chronicle-demo-token-do-not-ship}
export MESH_HOST=${MESH_HOST:-127.0.0.1}
export MESH_PORT=${MESH_PORT:-8765}
export MESH_CORE_URL=${MESH_CORE_URL:-http://${MESH_HOST}:${MESH_PORT}}
export CHRONICLE_INSPECTOR_PORT=${CHRONICLE_INSPECTOR_PORT:-9100}
