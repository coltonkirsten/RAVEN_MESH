"""Test fixtures: boot Core in-process with the chronicle demo manifest, then
register echo_capability + mesh_chronicle alongside in the same event loop."""
from __future__ import annotations

import asyncio
import hashlib
import os
import pathlib
import socket
import sys

import pytest
import pytest_asyncio
from aiohttp import web

EXP = pathlib.Path(__file__).resolve().parent.parent
REPO = EXP.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(EXP))

# Core requires a non-default admin token to start.
os.environ.setdefault("ADMIN_TOKEN", "test-chronicle-token")


def _derive(name: str) -> str:
    return hashlib.sha256(f"mesh:{name}:dev".encode()).hexdigest()


def _set_demo_secrets() -> None:
    for nid in ("client_actor", "echo_capability", "mesh_chronicle"):
        os.environ.setdefault(f"{nid.upper()}_SECRET", _derive(nid))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def core_server(tmp_path):
    _set_demo_secrets()
    audit_path = tmp_path / "audit.log"
    os.environ["AUDIT_LOG"] = str(audit_path)
    manifest = EXP / "manifests" / "chronicle_demo_v1.yaml"
    from core.core import make_app
    app = make_app(str(manifest), str(audit_path))
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base_url = f"http://127.0.0.1:{port}"
    yield {
        "app": app,
        "url": base_url,
        "manifest": str(manifest),
        "v2_manifest": str(EXP / "manifests" / "chronicle_demo_v2.yaml"),
        "audit_path": audit_path,
        "state": app["state"],
        "tmp_path": tmp_path,
    }
    await runner.cleanup()
