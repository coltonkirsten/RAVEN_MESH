#!/bin/bash
# Container entrypoint. We need to symlink ~/.claude into the persistent
# ledger volume so claude's session state (sessions/, projects/, etc.)
# survives `--rm`. Without this, --resume <session_id> always 404s on the
# second container run.
set -e

CLAUDE_PERSIST_DIR="/agent/ledger/.claude"
HOME_CLAUDE="$HOME/.claude"

mkdir -p "$CLAUDE_PERSIST_DIR"
# If $HOME/.claude already exists (e.g. from the image), nuke it — we want
# the link, not whatever the base image laid down.
rm -rf "$HOME_CLAUDE"
ln -sfn "$CLAUDE_PERSIST_DIR" "$HOME_CLAUDE"

exec claude "$@"
