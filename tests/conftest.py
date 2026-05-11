"""Shared fixtures: boot Core in-process on a free port for each test.

The default ``core_server`` fixture builds an ephemeral manifest with
four nodes — ``voice_actor`` (actor), ``tasks`` (capability with create
and list surfaces), ``human_approval`` (approval), and ``external_node``
(capability) — plus the edges historically exercised by protocol-tier
tests. The shape is constructed in-process: no test depends on any
shipped manifest or schema file.

Tests that need a different mesh shape should import the helpers from
``tests/_mesh_helpers.py`` and call ``make_core_app`` directly with
their own ephemeral manifest.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest
import pytest_asyncio
from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_ADMIN_TOKEN = "test-admin-token-do-not-ship"
os.environ.setdefault("ADMIN_TOKEN", TEST_ADMIN_TOKEN)

from core.core import make_app  # noqa: E402

from tests._mesh_helpers import (  # noqa: E402
    build_ephemeral_manifest,
    core_edges_for,
    free_port,
    minimal_actor,
    minimal_approval,
    minimal_capability,
    minimal_surface,
)


def _set_demo_secrets() -> None:
    for var in [
        "VOICE_SECRET", "TASKS_SECRET", "HUMAN_APPROVAL_SECRET", "EXTERNAL_NODE_SECRET",
    ]:
        os.environ.setdefault(var, f"test-secret-{var}")


def _build_demo_shaped_manifest(tmp_path: pathlib.Path) -> pathlib.Path:
    """Construct the four-node demo-shaped mesh inline. No demo.yaml read.

    The shape this builds:
      - voice_actor (actor) with an inbox surface, granted edges to every
        core.* surface plus tasks.list, human_approval.inbox, external_node.ping.
      - tasks (capability) exposing create + list tool surfaces.
      - human_approval (approval) exposing a request_response inbox.
      - external_node (capability) exposing a ping tool surface.
    """
    nodes = [
        minimal_actor(
            "voice_actor",
            secret_env="VOICE_SECRET",
            surfaces=[minimal_surface(
                "inbox",
                schema_path="../schemas/voice_inbox.json",
                type_="inbox",
                invocation_mode="fire_and_forget",
            )],
        ),
        minimal_capability(
            "tasks",
            secret_env="TASKS_SECRET",
            surfaces=[
                minimal_surface("create", schema_path="../schemas/task_create.json"),
                minimal_surface("list", schema_path="../schemas/task_list.json"),
            ],
        ),
        minimal_approval(
            "human_approval",
            secret_env="HUMAN_APPROVAL_SECRET",
            surfaces=[minimal_surface(
                "inbox",
                schema_path="../schemas/approval_request.json",
                type_="inbox",
                invocation_mode="request_response",
            )],
        ),
        minimal_capability(
            "external_node",
            secret_env="EXTERNAL_NODE_SECRET",
            surfaces=[minimal_surface("ping", schema_path="../schemas/echo.json")],
        ),
    ]
    edges: list[tuple[str, str]] = [
        ("voice_actor", "tasks.list"),
        ("voice_actor", "human_approval.inbox"),
        ("human_approval", "tasks.create"),
        ("voice_actor", "external_node.ping"),
    ]
    edges.extend(core_edges_for("voice_actor"))
    schemas = {
        "task_create.json": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string"}},
            "additionalProperties": True,
        },
    }
    return build_ephemeral_manifest(tmp_path, nodes, edges, schemas=schemas)


@pytest_asyncio.fixture
async def core_server(tmp_path):
    """Boot Core against an ephemeral demo-shaped mesh on a free port.

    The manifest is written into ``tmp_path/manifests/test.yaml`` with
    accompanying schema files under ``tmp_path/schemas``. No file in the
    repo is read or mutated by this fixture.
    """
    _set_demo_secrets()
    audit_path = tmp_path / "audit.log"
    os.environ["AUDIT_LOG"] = str(audit_path)
    manifest_path = _build_demo_shaped_manifest(tmp_path)
    app = make_app(str(manifest_path), str(audit_path))
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base_url = f"http://127.0.0.1:{port}"
    yield {
        "app": app,
        "url": base_url,
        "audit_path": audit_path,
        "state": app["state"],
        "manifest_path": manifest_path,
    }
    await runner.cleanup()
