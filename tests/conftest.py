"""Shared fixtures: boot Core in-process on a free port for each test session."""
from __future__ import annotations

import asyncio
import os
import pathlib
import socket
import sys

import pytest
import pytest_asyncio
from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.core import make_app  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _set_demo_secrets() -> None:
    # Deterministic per-node secrets for tests.
    for var in [
        "VOICE_SECRET", "TASKS_SECRET", "HUMAN_APPROVAL_SECRET", "EXTERNAL_NODE_SECRET",
    ]:
        os.environ.setdefault(var, f"test-secret-{var}")


@pytest_asyncio.fixture
async def core_server(tmp_path):
    """Boots Core on a fresh port with a per-test audit log."""
    _set_demo_secrets()
    audit_path = tmp_path / "audit.log"
    os.environ["AUDIT_LOG"] = str(audit_path)
    manifest = ROOT / "manifests" / "demo.yaml"
    app = make_app(str(manifest), str(audit_path))
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base_url = f"http://127.0.0.1:{port}"
    yield {"app": app, "url": base_url, "audit_path": audit_path, "state": app["state"]}
    await runner.cleanup()
