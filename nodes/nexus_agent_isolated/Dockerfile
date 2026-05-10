# nexus_agent_isolated — claude + a tiny python MCP bridge in one image.
#
# Built once per host. The host process (nodes/nexus_agent_isolated/agent.py)
# spawns this image with `docker run` per inbox message. Inside, claude is the
# entrypoint and our bridge is wired in via --mcp-config.
#
# Auth note: the host extracts the macOS keychain OAuth token and passes it
# in as CLAUDE_CODE_OAUTH_TOKEN. This image does not contain credentials.

FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# Install the bridge's Python dependencies into the system site-packages.
# (PEP 668 means we either use --break-system-packages or a venv. Image is
# isolated so --break-system-packages is fine.)
RUN pip3 install --no-cache-dir --break-system-packages \
        aiohttp \
        mcp

# Bridge + MCP config get baked into the image — the container is self-
# contained. The host doesn't mount any code into it.
RUN mkdir -p /etc/agent /usr/local/bin
COPY mcp_bridge.py /etc/agent/mcp_bridge.py
COPY mcp.json /etc/agent/mcp.json
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Non-root user. claude refuses to run with --dangerously-skip-permissions
# as root, so we drop privileges. The base image's `node` user (uid 1000)
# already exists — reuse it instead of creating a duplicate.
RUN mkdir -p /workspace /agent/ledger \
    && chown -R node:node /workspace /agent /etc/agent

WORKDIR /workspace
USER node

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
