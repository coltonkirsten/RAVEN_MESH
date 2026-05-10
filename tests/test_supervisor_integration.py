"""Integration tests for the supervisor admin endpoints.

These tests boot Core with `enable_supervisor=True` and exercise the
/v0/admin/{spawn,stop,restart,reconcile,processes} endpoints end-to-end.
We use a temporary manifest with sleep-based "nodes" so we don't need to
boot real mesh nodes.
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

ADMIN_TOKEN = "test-admin-token-do-not-ship"
HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def supervisor_core(tmp_path):
    """Boot Core with supervisor enabled and a custom manifest of sleep nodes."""
    repo_root = tmp_path
    (repo_root / "scripts").mkdir()
    (repo_root / "manifests").mkdir()
    (repo_root / "schemas").mkdir()

    # Trivial schema for a "noop" surface.
    schema_path = repo_root / "schemas" / "noop.json"
    schema_path.write_text(json.dumps({
        "type": "object",
        "additionalProperties": True,
    }))

    # Build run scripts that write a heartbeat file, then sleep. We write a
    # tiny Python helper next to the script so we don't have to fight with
    # shell quoting.
    helper = repo_root / "scripts" / "_node_stub.py"
    helper.write_text(
        "import os, sys, time, pathlib\n"
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

    # Manifest declares all three nodes.
    manifest_path = repo_root / "manifests" / "test.yaml"
    manifest_path.write_text(yaml.safe_dump({
        "version": "v0",
        "nodes": [
            {"id": nid, "kind": "capability", "identity_secret": f"test-{nid}",
             "surfaces": [{"name": "noop", "type": "tool",
                           "schema": "../schemas/noop.json"}]}
            for nid in ("alpha", "beta", "gamma")
        ],
        "relationships": [],
    }))

    audit_path = repo_root / "audit.log"
    log_dir = repo_root / "logs"

    # Set ADMIN_TOKEN before make_app reads it
    os.environ.setdefault("ADMIN_TOKEN", ADMIN_TOKEN)

    # cd into the temp repo so the script_resolver finds scripts/run_*.sh
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
        try:
            yield {
                "url": base_url,
                "state": app["state"],
                "manifest_path": manifest_path,
                "repo_root": repo_root,
            }
        finally:
            # Make sure all spawned children are killed
            sup = app["state"].supervisor
            if sup is not None:
                await sup.shutdown_all(timeout=2.0)
            await runner.cleanup()
    finally:
        os.chdir(old_cwd)


async def _post(session, url, **kwargs):
    async with session.post(url, headers=HEADERS, **kwargs) as r:
        body = await r.json()
        return r.status, body


async def _get(session, url):
    async with session.get(url, headers=HEADERS) as r:
        body = await r.json()
        return r.status, body


# ---------- tests ----------

async def test_processes_empty_initially(supervisor_core):
    async with aiohttp.ClientSession() as s:
        status, body = await _get(s, supervisor_core["url"] + "/v0/admin/processes")
    assert status == 200
    assert body["supervisor_enabled"] is True
    assert body["processes"] == []


async def test_spawn_one_node(supervisor_core):
    async with aiohttp.ClientSession() as s:
        status, body = await _post(s, supervisor_core["url"] + "/v0/admin/spawn",
                                    json={"node_id": "alpha"})
        assert status == 200, body
        assert body["ok"] is True
        assert body["child"]["status"] == "running"
        assert body["child"]["pid"] > 0

        # Heartbeat file should appear (the script writes it on boot)
        await asyncio.sleep(0.5)
        hb = supervisor_core["repo_root"] / "alpha.heartbeat"
        assert hb.exists()


async def test_spawn_unknown_node(supervisor_core):
    async with aiohttp.ClientSession() as s:
        status, body = await _post(s, supervisor_core["url"] + "/v0/admin/spawn",
                                    json={"node_id": "ghost"})
    assert status == 404
    assert body["error"] == "unknown_node"


async def test_spawn_then_stop(supervisor_core):
    async with aiohttp.ClientSession() as s:
        await _post(s, supervisor_core["url"] + "/v0/admin/spawn",
                    json={"node_id": "alpha"})
        status, body = await _post(s, supervisor_core["url"] + "/v0/admin/stop",
                                    json={"node_id": "alpha"})
        assert status == 200, body
        assert body["ok"] is True
        # Verify processes list reflects stopped state
        _, plist = await _get(s, supervisor_core["url"] + "/v0/admin/processes")
        statuses = {p["node_id"]: p["status"] for p in plist["processes"]}
        assert statuses.get("alpha") == "stopped"


async def test_restart_changes_pid(supervisor_core):
    async with aiohttp.ClientSession() as s:
        _, b1 = await _post(s, supervisor_core["url"] + "/v0/admin/spawn",
                             json={"node_id": "alpha"})
        pid1 = b1["child"]["pid"]
        _, b2 = await _post(s, supervisor_core["url"] + "/v0/admin/restart",
                             json={"node_id": "alpha"})
        pid2 = b2["child"]["pid"]
        assert pid1 != pid2


async def test_reconcile_spawns_all_manifest_nodes(supervisor_core):
    async with aiohttp.ClientSession() as s:
        status, body = await _post(s, supervisor_core["url"] + "/v0/admin/reconcile",
                                    json={})
        assert status == 200, body
        spawned = sorted(body["actions"]["spawned"])
        assert spawned == ["alpha", "beta", "gamma"]

        # Verify all are running
        await asyncio.sleep(0.3)
        _, plist = await _get(s, supervisor_core["url"] + "/v0/admin/processes")
        running = {p["node_id"] for p in plist["processes"] if p["status"] == "running"}
        assert running == {"alpha", "beta", "gamma"}


async def test_reconcile_after_manifest_change(supervisor_core):
    """Edit manifest to remove one node, hit /reload + /reconcile, verify process stops."""
    async with aiohttp.ClientSession() as s:
        # Boot the full mesh
        await _post(s, supervisor_core["url"] + "/v0/admin/reconcile", json={})
        await asyncio.sleep(0.3)

        # Edit manifest: drop beta
        manifest_path = supervisor_core["manifest_path"]
        m = yaml.safe_load(manifest_path.read_text())
        m["nodes"] = [n for n in m["nodes"] if n["id"] != "beta"]
        manifest_path.write_text(yaml.safe_dump(m))

        # Reload manifest into Core, then reconcile
        await _post(s, supervisor_core["url"] + "/v0/admin/reload", json={})
        _, body = await _post(s, supervisor_core["url"] + "/v0/admin/reconcile", json={})
        assert "beta" in body["actions"]["stopped"]

        await asyncio.sleep(0.3)
        _, plist = await _get(s, supervisor_core["url"] + "/v0/admin/processes")
        statuses = {p["node_id"]: p["status"] for p in plist["processes"]}
        assert statuses["beta"] == "stopped"
        assert statuses["alpha"] == "running"
        assert statuses["gamma"] == "running"


async def test_unauthorized_request_rejected(supervisor_core):
    async with aiohttp.ClientSession() as s:
        async with s.get(supervisor_core["url"] + "/v0/admin/processes") as r:
            assert r.status == 401


async def test_metrics_endpoint_returns_per_child_data(supervisor_core):
    """GET /v0/admin/metrics exposes per-child + totals (PROTOCOL-LAYER, generic)."""
    async with aiohttp.ClientSession() as s:
        await _post(s, supervisor_core["url"] + "/v0/admin/spawn",
                    json={"node_id": "alpha"})
        await asyncio.sleep(0.3)

        status, body = await _get(s, supervisor_core["url"] + "/v0/admin/metrics")
        assert status == 200, body
        assert body["supervisor_enabled"] is True
        m = body["metrics"]
        assert m["totals"]["children"] == 1
        assert m["totals"]["running"] == 1
        assert m["totals"]["restarts"] == 0
        assert m["supervisor_uptime_seconds"] >= 0

        nodes = {c["node_id"]: c for c in m["children"]}
        assert "alpha" in nodes
        assert nodes["alpha"]["status"] == "running"
        assert nodes["alpha"]["uptime_seconds"] > 0
        assert nodes["alpha"]["restart_count_total"] == 0


async def test_metrics_endpoint_disabled_when_supervisor_off(tmp_path):
    """With supervisor disabled, /metrics returns supervisor_enabled=False."""
    audit_path = tmp_path / "audit.log"
    manifest = ROOT / "manifests" / "demo.yaml"
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
                body = await r.json()
                assert body["supervisor_enabled"] is False
                assert body["metrics"] is None
    finally:
        await runner.cleanup()


async def test_drain_endpoint_stops_running_child(supervisor_core):
    """POST /v0/admin/drain stops a running child after in-flight reaches 0."""
    async with aiohttp.ClientSession() as s:
        await _post(s, supervisor_core["url"] + "/v0/admin/spawn",
                    json={"node_id": "alpha"})
        await asyncio.sleep(0.2)

        status, body = await _post(s, supervisor_core["url"] + "/v0/admin/drain",
                                    json={"node_id": "alpha", "timeout": 5.0})
        assert status == 200, body
        assert body["ok"] is True
        assert body["timed_out"] is False
        assert body["drained_in_flight"] == 0

        await asyncio.sleep(0.1)
        _, plist = await _get(s, supervisor_core["url"] + "/v0/admin/processes")
        statuses = {p["node_id"]: p["status"] for p in plist["processes"]}
        assert statuses["alpha"] == "stopped"


async def test_drain_endpoint_unknown_node(supervisor_core):
    async with aiohttp.ClientSession() as s:
        status, body = await _post(s, supervisor_core["url"] + "/v0/admin/drain",
                                    json={"node_id": "ghost", "timeout": 1.0})
        assert status == 200
        assert body["ok"] is False
        assert body["error"] == "unknown_node"


async def test_supervisor_disabled_returns_409(tmp_path):
    """Without enable_supervisor=True, supervisor endpoints return 409."""
    audit_path = tmp_path / "audit.log"
    manifest = ROOT / "manifests" / "demo.yaml"
    app = make_app(str(manifest), str(audit_path), enable_supervisor=False)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{port}/v0/admin/spawn",
                              headers=HEADERS,
                              json={"node_id": "tasks"}) as r:
                assert r.status == 409
                body = await r.json()
                assert body["error"] == "supervisor_disabled"
    finally:
        await runner.cleanup()
