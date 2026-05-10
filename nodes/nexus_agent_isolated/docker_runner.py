"""Spawn the `claude` CLI inside a Docker container for one inbox message.

Mirrors nexus_agent.cli_runner.run_claude but the binary lives inside the
nexus_agent_isolated image. The host process drives docker, streams stdout
exactly the same way, and parses claude's stream-json output.

Auth: on macOS, the OAuth token lives in the keychain (service
'Claude Code-credentials'). We extract it via `security find-generic-password`
and pass it as CLAUDE_CODE_OAUTH_TOKEN. ANTHROPIC_API_KEY is also honored
as a fallback when set.

Networking: we use --add-host=host.docker.internal:host-gateway so the
in-container bridge can reach the host's loopback control server at
http://host.docker.internal:<control_port>.

Filesystem:
  /workspace        — empty (no host code visible)
  /agent/ledger     — named volume, persists memory across runs
  /etc/agent/...    — bridge + mcp.json, baked into the image
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
import subprocess
from typing import Any, Awaitable, Callable

log = logging.getLogger("nexus_agent_isolated.docker_runner")

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclasses.dataclass
class CliResult:
    result_text: str = ""
    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    is_error: bool = False
    exit_code: int | None = None


KEYCHAIN_SERVICE = "Claude Code-credentials"


def get_oauth_token_from_keychain() -> str | None:
    """Best-effort: pull the claude OAuth access token from macOS keychain."""
    if not shutil.which("security"):
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not out:
            return None
        data = json.loads(out)
        token = (data.get("claudeAiOauth") or {}).get("accessToken")
        return token if isinstance(token, str) and token else None
    except Exception:  # noqa: BLE001
        return None


def resolve_auth_env() -> dict[str, str]:
    """Pick the best available auth credential for the container.

    Priority: env CLAUDE_CODE_OAUTH_TOKEN → keychain → env ANTHROPIC_API_KEY.
    Returns the env dict to pass into `docker run -e`.
    """
    env: dict[str, str] = {}
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or get_oauth_token_from_keychain()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


async def run_claude_in_container(
    *,
    message: str,
    system_prompt: str,
    image: str,
    ledger_volume: str,
    control_port: int,
    control_token: str,
    model: str = "claude-sonnet-4-6",
    session_id: str | None = None,
    on_event: EventHandler | None = None,
    extra_run_args: list[str] | None = None,
) -> CliResult:
    """Spawn `docker run ... <image> <claude args>`, stream stdout, return CliResult."""
    if shutil.which("docker") is None:
        raise RuntimeError("`docker` not found on PATH")

    auth_env = resolve_auth_env()
    if not auth_env:
        raise RuntimeError(
            "no claude credentials found — set CLAUDE_CODE_OAUTH_TOKEN, "
            "ANTHROPIC_API_KEY, or run `claude` once to populate the keychain"
        )

    control_url = f"http://host.docker.internal:{control_port}"

    # Container env: bridge config + auth.
    docker_env = {
        "NEXUS_AGENT_CONTROL_URL": control_url,
        "NEXUS_AGENT_CONTROL_TOKEN": control_token,
        "NEXUS_AGENT_LEDGER_DIR": "/agent/ledger",
        "PYTHONUNBUFFERED": "1",
        **auth_env,
    }

    docker_args = [
        "docker", "run", "--rm", "-i",
        "--add-host=host.docker.internal:host-gateway",
        "-v", f"{ledger_volume}:/agent/ledger",
    ]
    for k, v in docker_env.items():
        docker_args += ["-e", f"{k}={v}"]
    if extra_run_args:
        docker_args += extra_run_args
    docker_args.append(image)

    claude_args = [
        "-p", message,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--mcp-config", "/etc/agent/mcp.json",
        "--strict-mcp-config",
        "--tools", "",
        "--system-prompt", system_prompt,
        "--dangerously-skip-permissions",
    ]
    if session_id:
        claude_args += ["--resume", session_id]

    args = docker_args + claude_args

    async def _emit(kind: str, data: dict[str, Any]) -> None:
        if on_event is not None:
            try:
                await on_event(kind, data)
            except Exception:  # noqa: BLE001
                log.exception("on_event raised")

    # Redact secrets from the spawn event.
    safe_args = []
    for a in args:
        if "CLAUDE_CODE_OAUTH_TOKEN=" in a or "ANTHROPIC_API_KEY=" in a:
            safe_args.append(a.split("=", 1)[0] + "=***")
        elif len(a) > 200:
            safe_args.append(f"{a[:200]}…({len(a)} chars)")
        else:
            safe_args.append(a)
    await _emit("cli_spawn", {
        "args": safe_args,
        "image": image,
        "ledger_volume": ledger_volume,
        "control_url": control_url,
        "model": model,
    })

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    result = CliResult(session_id=session_id)

    async def _drain_stdout() -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                await _emit("cli_output", {"raw": line})
                continue
            await _emit("agent_message", msg)
            mtype = msg.get("type")
            if mtype == "system" and msg.get("subtype") == "init" and msg.get("session_id"):
                result.session_id = msg["session_id"]
            elif mtype == "result":
                if msg.get("result"):
                    result.result_text = msg["result"]
                if msg.get("is_error"):
                    result.is_error = True
                    errors = msg.get("errors")
                    if isinstance(errors, list) and errors:
                        result.result_text = "; ".join(str(e) for e in errors)
                usage = msg.get("usage") or {}
                result.input_tokens += int(usage.get("input_tokens", 0) or 0)
                result.output_tokens += int(usage.get("output_tokens", 0) or 0)

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                await _emit("cli_stderr", {"line": line})

    await asyncio.gather(_drain_stdout(), _drain_stderr())
    result.exit_code = await proc.wait()
    await _emit("cli_exit", {"code": result.exit_code})

    if result.exit_code != 0 and not result.result_text:
        result.is_error = True
        result.result_text = f"docker/claude exited with code {result.exit_code}"

    return result


def ensure_volume(name: str) -> None:
    """Create the named docker volume if it doesn't exist (no-op if it does)."""
    if shutil.which("docker") is None:
        return
    try:
        subprocess.run(
            ["docker", "volume", "inspect", name],
            check=True, capture_output=True, timeout=5,
        )
    except subprocess.CalledProcessError:
        subprocess.run(
            ["docker", "volume", "create", name],
            check=True, capture_output=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        pass
