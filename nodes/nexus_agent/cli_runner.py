"""Spawn the `claude` CLI as a subprocess for one inbox message.

Mirrors the structure of NEXUS's cli-runner.ts. We invoke claude with
--output-format stream-json, parse each line, and surface every event back
through the on_event callback so the inspector UI can stream it live.

Authentication: claude reads the host's macOS keychain OAuth tokens by
default. We pass through the parent environment unchanged.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import pathlib
import shutil
import tempfile
from typing import Any, Awaitable, Callable

log = logging.getLogger("nexus_agent.cli_runner")

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclasses.dataclass
class CliResult:
    result_text: str = ""
    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    is_error: bool = False
    exit_code: int | None = None


def write_mcp_config(
    bridge_path: pathlib.Path,
    control_url: str,
    control_token: str,
    ledger_dir: pathlib.Path,
    extra_env: dict[str, str] | None = None,
) -> pathlib.Path:
    """Write the --mcp-config JSON that points claude at our stdio bridge."""
    env: dict[str, str] = {
        "NEXUS_AGENT_CONTROL_URL": control_url,
        "NEXUS_AGENT_CONTROL_TOKEN": control_token,
        "NEXUS_AGENT_LEDGER_DIR": str(ledger_dir),
        "PYTHONUNBUFFERED": "1",
    }
    if extra_env:
        env.update(extra_env)

    config = {
        "mcpServers": {
            "nexus_agent_bridge": {
                "command": "python3",
                "args": [str(bridge_path)],
                "env": env,
            }
        }
    }
    out = pathlib.Path(tempfile.gettempdir()) / "nexus_agent_mcp_config.json"
    out.write_text(json.dumps(config, indent=2))
    return out


async def run_claude(
    *,
    message: str,
    system_prompt: str,
    bridge_path: pathlib.Path,
    control_url: str,
    control_token: str,
    ledger_dir: pathlib.Path,
    model: str = "claude-sonnet-4-6",
    session_id: str | None = None,
    on_event: EventHandler | None = None,
    cwd: pathlib.Path | None = None,
    extra_bridge_env: dict[str, str] | None = None,
) -> CliResult:
    """Spawn claude, stream stdout, return the parsed result."""
    if shutil.which("claude") is None:
        raise RuntimeError("`claude` CLI not found on PATH")

    mcp_config = write_mcp_config(
        bridge_path=bridge_path,
        control_url=control_url,
        control_token=control_token,
        ledger_dir=ledger_dir,
        extra_env=extra_bridge_env,
    )

    args = [
        "claude",
        "-p", message,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--mcp-config", str(mcp_config),
        "--system-prompt", system_prompt,
        "--dangerously-skip-permissions",
    ]
    if session_id:
        args += ["--resume", session_id]

    async def _emit(kind: str, data: dict[str, Any]) -> None:
        if on_event is not None:
            try:
                await on_event(kind, data)
            except Exception:  # noqa: BLE001
                log.exception("on_event raised")

    await _emit("cli_spawn", {
        "args": [a if len(a) < 200 else f"{a[:200]}…({len(a)} chars)" for a in args],
        "mcp_config": str(mcp_config),
        "model": model,
    })

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=os.environ.copy(),
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
        result.result_text = f"claude exited with code {result.exit_code}"

    return result
