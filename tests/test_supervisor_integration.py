"""Integration tests for the supervisor surfaces (now reached as ``core.*``).

These tests boot Core with ``enable_supervisor=True`` and exercise the
``core.{processes, spawn, stop, restart, reconcile, drain, metrics}``
surfaces end-to-end via ``/v0/invoke``. They use a temporary manifest
with sleep-based "nodes" so we don't need to boot real mesh nodes.

A bootstrap node ``operator`` is added to the temporary manifest so the
tests can sign envelopes and invoke ``core.*``.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import sys

import aiohttp
import pytest
import pytest_asyncio
import yaml
from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.core import make_app  # noqa: E402
from node_sdk import MeshNode  # noqa: E402

ADMIN_TOKEN = "test-admin-token-do-not-ship"
HEADERS = {"X-Admin-Token": ADMIN_TOKEN}
OPERATOR_SECRET = "test-operator-secret"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_manifest(repo_root: pathlib.Path) -> pathlib.Path:
    """Write a manifest with three sleep-stub nodes plus an operator."""
    (repo_root / "scripts").mkdir()
    (repo_root / "manifests").mkdir()
    (repo_root / "schemas").mkdir()

    schema_path = repo_root / "schemas" / "noop.json"
    schema_path.write_text(json.dumps({
        "type": "object",
        "additionalProperties": True,
    }))

    helper = repo_root / "scripts" / "_node_stub.py"
    helper.write_text(
        "import os, time, pathlib\n"
        "nid = os.environ.get('MESH_NODE_ID', 'unknown')\n"
        "root = pathlib.Path(__file__).resolve().parent.parent\n"
        "(root / f'{nid}.heartbeat').write_text(str(time.time()))\n"
        "time.sleep(120)\n"
    )
    for nid in ("alpha", "beta", "gamma"):
        script = repo_root / "scripts" / f"run_{nid}.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'exec python3 "$(dirname "$0")/_node_stub.py"\n'
        )
        script.chmod(0o755)

    nodes = [
        {"id": nid, "kind": "capability", "identity_secret": f"test-{nid}",
         "surfaces": [{"name": "noop", "type": "tool",
                       "schema": "../schemas/noop.json"}]}
        for nid in ("alpha", "beta", "gamma")
    ]
    nodes.append({
        "id": "operator", "kind": "actor", "identity_secret": OPERATOR_SECRET,
        "runtime": "external-http",
        "surfaces": [],
    })
    relationships = [
        {"from": "operator", "to": f"core.{s}"}
        for s in ("state", "processes", "metrics", "audit_query",
                  "set_manifest", "reload_manifest",
                  "spawn", "stop", "restart", "reconcile", "drain")
    ]
    manifest_path = repo_root / "manifests" / "test.yaml"
    manifest_path.write_text(yaml.safe_dump({
        "version": "v0",
        "nodes": nodes,
        "relationships": relationships,
    }))
    return manifest_path


@pytest_asyncio.fixture
async def supervisor_core(tmp_path):
    """Boot Core with supervisor enabled and a custom manifest of sleep nodes."""
    repo_root = tmp_path
    manifest_path = _build_manifest(repo_root)

    audit_path = repo_root / "audit.log"
    log_dir = repo_root / "logs"

    os.environ.setdefault("ADMIN_TOKEN", ADMIN_TOKEN)

    old_cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        app = make_app(
            str(manifest_path),
            str(audit_path),
            enable_supervisor=True,
            supervisor_log_dir=str(log_dir),
        )
        runner = web.AppRunner(app)
        await runner.setup()
        port = _free_port()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        base_url = f"http://127.0.0.1:{port}"

        operator = MeshNode(node_id="operator", secret=OPERATOR_SECRET,
                             core_url=base_url)
        await operator.connect()
        try:
            yield {
                "url": base_url,
                "state": app["state"],
                "manifest_path": manifest_path,
                "repo_root": repo_root,
                "operator": operator,
            }
        finally:
            await operator.stop()
            sup = app["state"].supervisor
            if sup is not None:
                await sup.shutdown_all(timeout=2.0)
            await runner.cleanup()
    finally:
        os.chdir(old_cwd)


async def _invoke(operator: MeshNode, surface: str, payload: dict | None = None) -> dict:
    """Invoke ``core.<surface>`` and return the response envelope's payload."""
    env = await operator.invoke(f"core.{surface}", payload or {})
    assert env["kind"] in ("response", "error"), env
    return env["payload"] if env["kind"] == "response" else env


# ---------- tests ----------

async def test_processes_empty_initially(supervisor_core):
    body = await _invoke(supervisor_core["operator"], "processes")
    assert body["supervisor_enabled"] is True
    assert body["processes"] == []


async def test_spawn_one_node(supervisor_core):
    body = await _invoke(supervisor_core["operator"], "spawn", {"node_id": "alpha"})
    assert body["ok"] is True
    assert body["child"]["status"] == "running"
    assert body["child"]["pid"] > 0
    await asyncio.sleep(0.5)
    hb = supervisor_core["repo_root"] / "alpha.heartbeat"
    assert hb.exists()


async def test_spawn_unknown_node(supervisor_core):
    env = await supervisor_core["operator"].invoke(
        "core.spawn", {"node_id": "ghost"}
    )
    assert env["kind"] == "error"
    assert env["payload"]["error"] == "unknown_node"


async def test_spawn_then_stop(supervisor_core):
    op = supervisor_core["operator"]
    await _invoke(op, "spawn", {"node_id": "alpha"})
    body = await _invoke(op, "stop", {"node_id": "alpha"})
    assert body["ok"] is True
    plist = await _invoke(op, "processes")
    statuses = {p["node_id"]: p["status"] for p in plist["processes"]}
    assert statuses.get("alpha") == "stopped"


async def test_restart_changes_pid(supervisor_core):
    op = supervisor_core["operator"]
    b1 = await _invoke(op, "spawn", {"node_id": "alpha"})
    pid1 = b1["child"]["pid"]
    b2 = await _invoke(op, "restart", {"node_id": "alpha"})
    pid2 = b2["child"]["pid"]
    assert pid1 != pid2


async def test_reconcile_spawns_all_manifest_nodes(supervisor_core):
    op = supervisor_core["operator"]
    body = await _invoke(op, "reconcile")
    spawned = sorted(body["actions"]["spawned"])
    assert spawned == ["alpha", "beta", "gamma"]
    await asyncio.sleep(0.3)
    plist = await _invoke(op, "processes")
    running = {p["node_id"] for p in plist["processes"] if p["status"] == "running"}
    assert running == {"alpha", "beta", "gamma"}


async def test_reconcile_after_manifest_change(supervisor_core):
    """Edit manifest to remove one node, hit core.reload_manifest +
    core.reconcile, verify process stops.
    """
    op = supervisor_core["operator"]
    await _invoke(op, "reconcile")
    await asyncio.sleep(0.3)

    manifest_path = supervisor_core["manifest_path"]
    m = yaml.safe_load(manifest_path.read_text())
    m["nodes"] = [n for n in m["nodes"] if n["id"] != "beta"]
    manifest_path.write_text(yaml.safe_dump(m))

    await _invoke(op, "reload_manifest")
    body = await _invoke(op, "reconcile")
    assert "beta" in body["actions"]["stopped"]

    await asyncio.sleep(0.3)
    plist = await _invoke(op, "processes")
    statuses = {p["node_id"]: p["status"] for p in plist["processes"]}
    assert statuses["beta"] == "stopped"
    assert statuses["alpha"] == "running"
    assert statuses["gamma"] == "running"


async def test_metrics_endpoint_returns_per_child_data(supervisor_core):
    """core.metrics surfaces aggregate + per-child supervisor counters."""
    op = supervisor_core["operator"]
    await _invoke(op, "spawn", {"node_id": "alpha"})
    await asyncio.sleep(0.3)

    body = await _invoke(op, "metrics")
    sup = body["supervisor"]
    assert sup is not None
    assert sup["totals"]["children"] == 1
    assert sup["totals"]["running"] == 1
    assert sup["totals"]["restarts"] == 0
    assert sup["supervisor_uptime_seconds"] >= 0

    nodes = {c["node_id"]: c for c in sup["children"]}
    assert "alpha" in nodes
    assert nodes["alpha"]["status"] == "running"
    assert nodes["alpha"]["uptime_seconds"] > 0
    assert nodes["alpha"]["restart_count_total"] == 0


async def test_admin_metrics_prometheus_includes_supervisor(supervisor_core):
    """Prometheus exposition picks up supervisor totals when supervisor is on."""
    url = supervisor_core["url"]
    op = supervisor_core["operator"]
    await _invoke(op, "spawn", {"node_id": "alpha"})
    await asyncio.sleep(0.3)
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/v0/admin/metrics", headers=HEADERS) as r:
            body = await r.text()
    assert "mesh_supervisor_children_total" in body
    assert "mesh_supervisor_child_uptime_seconds" in body
    assert 'node_id="alpha"' in body


async def test_drain_endpoint_stops_running_child(supervisor_core):
    """core.drain stops a running child after in-flight reaches 0."""
    op = supervisor_core["operator"]
    await _invoke(op, "spawn", {"node_id": "alpha"})
    await asyncio.sleep(0.2)

    body = await _invoke(op, "drain", {"node_id": "alpha", "timeout": 5.0})
    assert body["ok"] is True
    assert body["timed_out"] is False
    assert body["drained_in_flight"] == 0

    await asyncio.sleep(0.1)
    plist = await _invoke(op, "processes")
    statuses = {p["node_id"]: p["status"] for p in plist["processes"]}
    assert statuses["alpha"] == "stopped"


async def test_drain_endpoint_unknown_node(supervisor_core):
    env = await supervisor_core["operator"].invoke(
        "core.drain", {"node_id": "ghost", "timeout": 1.0}
    )
    assert env["kind"] == "response"
    assert env["payload"]["ok"] is False
    assert env["payload"]["error"] == "unknown_node"


async def test_supervisor_disabled_returns_error_envelope(tmp_path):
    """Without enable_supervisor=True, core.spawn returns an error envelope."""
    repo_root = tmp_path
    manifest_path = _build_manifest(repo_root)
    audit_path = repo_root / "audit.log"
    os.environ.setdefault("ADMIN_TOKEN", ADMIN_TOKEN)

    old_cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        app = make_app(str(manifest_path), str(audit_path),
                        enable_supervisor=False)
        runner = web.AppRunner(app)
        await runner.setup()
        port = _free_port()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        base_url = f"http://127.0.0.1:{port}"
        try:
            op = MeshNode(node_id="operator", secret=OPERATOR_SECRET,
                            core_url=base_url)
            await op.connect()
            try:
                env = await op.invoke("core.spawn", {"node_id": "alpha"})
                assert env["kind"] == "error"
                assert env["payload"]["error"] == "supervisor_disabled"
            finally:
                await op.stop()
        finally:
            await runner.cleanup()
    finally:
        os.chdir(old_cwd)


async def test_metrics_endpoint_disabled_when_supervisor_off(tmp_path):
    """With supervisor disabled, /v0/admin/metrics still exposes Core-only gauges."""
    audit_path = tmp_path / "audit.log"
    # Ephemeral manifest — same _build_manifest the supervised-tests use,
    # except we boot Core with enable_supervisor=False.
    manifest = _build_manifest(tmp_path)
    app = make_app(str(manifest), str(audit_path), enable_supervisor=False)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/v0/admin/metrics",
                             headers=HEADERS) as r:
                assert r.status == 200
                body = await r.text()
        assert "mesh_nodes_declared" in body
        assert "mesh_supervisor_uptime_seconds" not in body
    finally:
        await runner.cleanup()
