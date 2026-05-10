#!/usr/bin/env bash
# Source me. Provides deterministic per-node secrets so every script in this
# directory works without manual env-var setup.

# Each node's secret is a derived hash of "mesh:<NODE_ID>:dev" — stable across
# runs but unique per node.
_derive() { printf "mesh:%s:dev" "$1" | shasum -a 256 | cut -d' ' -f1; }

export VOICE_SECRET=${VOICE_SECRET:-$(_derive voice_actor)}
export TASKS_SECRET=${TASKS_SECRET:-$(_derive tasks)}
export HUMAN_APPROVAL_SECRET=${HUMAN_APPROVAL_SECRET:-$(_derive human_approval)}
export EXTERNAL_NODE_SECRET=${EXTERNAL_NODE_SECRET:-$(_derive external_node)}

export DUMMY_ACTOR_SECRET=${DUMMY_ACTOR_SECRET:-$(_derive dummy_actor)}
export APPROVAL_NODE_SECRET=${APPROVAL_NODE_SECRET:-$(_derive approval_node)}
export CRON_NODE_SECRET=${CRON_NODE_SECRET:-$(_derive cron_node)}
export WEBUI_NODE_SECRET=${WEBUI_NODE_SECRET:-$(_derive webui_node)}
export HUMAN_NODE_SECRET=${HUMAN_NODE_SECRET:-$(_derive human_node)}
export KANBAN_NODE_SECRET=${KANBAN_NODE_SECRET:-$(_derive kanban_node)}
export NEXUS_AGENT_SECRET=${NEXUS_AGENT_SECRET:-$(_derive nexus_agent)}
export NEXUS_AGENT_ISOLATED_SECRET=${NEXUS_AGENT_ISOLATED_SECRET:-$(_derive nexus_agent_isolated)}

export MESH_HOST=${MESH_HOST:-127.0.0.1}
export MESH_PORT=${MESH_PORT:-8000}
export MESH_CORE_URL=${MESH_CORE_URL:-http://${MESH_HOST}:${MESH_PORT}}
