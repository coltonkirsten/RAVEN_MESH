"""Unit tests for core/supervisor.py.

Tests use real subprocesses (small Python one-liners) so we exercise the actual
asyncio.create_subprocess_exec path, signal handling, and child monitoring.
Tests are scoped to the supervisor itself — wiring into core/core.py is
exercised by test_supervisor_integration.py.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import pathlib

import pytest

# Make the project root importable when tests are run from anywhere.
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.supervisor import ChildSpec, Supervisor


@pytest.fixture
def tmp_logs(tmp_path):
    return str(tmp_path / "logs")


def _spec(node_id: str, log_dir: str, *, py: str, restart: str = "permanent",
          max_restarts: int = 3, restart_window_s: float = 5.0) -> ChildSpec:
    """Helper to make a ChildSpec that runs a Python one-liner."""
    log_path = pathlib.Path(log_dir) / f"{node_id}.log"
    return ChildSpec(
        node_id=node_id,
        cmd=[sys.executable, "-c", py],
        env={},
        cwd=str(pathlib.Path.cwd()),
        log_path=str(log_path),
        restart=restart,
        max_restarts=max_restarts,
        restart_window_s=restart_window_s,
    )


@pytest.mark.asyncio
async def test_spawn_and_stop(tmp_logs):
    """Supervisor can start a child and stop it cleanly."""
    sup = Supervisor(runner_resolver=lambda nid, m: None, log_dir=tmp_logs)
    spec = _spec("sleeper", tmp_logs, py="import time; time.sleep(60)")

    res = await sup._start_locked(spec)  # using internal API; lock not needed in single-task test
    assert res["ok"], res
    pid = res["child"]["pid"]
    assert pid > 0
    assert sup.children["sleeper"].status == "running"
    # Process really exists
    os.kill(pid, 0)

    stop_res = await sup.stop("sleeper")
    assert stop_res["ok"], stop_res
    assert sup.children["sleeper"].status == "stopped"
    # PID is gone
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_unknown_node_stop(tmp_logs):
    sup = Supervisor(runner_resolver=lambda nid, m: None, log_dir=tmp_logs)
    res = await sup.stop("does_not_exist")
    assert res["ok"] is False
    assert res["error"] == "unknown_node"


@pytest.mark.asyncio
async def test_no_runner_resolver(tmp_logs):
    """spawn() returns an error when the resolver can't find a runner."""
    sup = Supervisor(runner_resolver=lambda nid, m: None, log_dir=tmp_logs)
    res = await sup.spawn("ghost", {})
    assert res["ok"] is False
    assert res["error"] == "no_runner"


@pytest.mark.asyncio
async def test_already_running_no_double_spawn(tmp_logs):
    """Spawning the same node twice doesn't create a second process."""
    spec = _spec("only_one", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    r1 = await sup.spawn("only_one", {})
    assert r1["ok"]
    pid1 = r1["child"]["pid"]

    r2 = await sup.spawn("only_one", {})
    assert r2["ok"]
    assert r2.get("already_running") is True
    pid2 = r2["child"]["pid"]
    assert pid1 == pid2

    await sup.stop("only_one")


@pytest.mark.asyncio
async def test_restart_replaces_process(tmp_logs):
    spec = _spec("restartable", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    r1 = await sup.spawn("restartable", {})
    pid1 = r1["child"]["pid"]

    r2 = await sup.restart("restartable", {})
    assert r2["ok"]
    pid2 = r2["child"]["pid"]
    assert pid1 != pid2

    await sup.stop("restartable")


@pytest.mark.asyncio
async def test_permanent_child_auto_restarts_on_crash(tmp_logs):
    """A permanent child that exits non-zero gets restarted automatically."""
    # Touch a marker file then exit 1 — we'll count touches to verify restart.
    marker = pathlib.Path(tmp_logs) / "marker.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    py = (
        f"import pathlib; "
        f"p = pathlib.Path({str(marker)!r}); "
        "p.parent.mkdir(parents=True, exist_ok=True); "
        "n = int(p.read_text()) + 1 if p.exists() else 1; "
        "p.write_text(str(n)); "
        "import sys; sys.exit(1)"
    )
    spec = _spec("crasher", tmp_logs, py=py, restart="permanent",
                 max_restarts=10, restart_window_s=30.0)
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    await sup.spawn("crasher", {})
    # Wait for several restarts (each takes 0.5s backoff after the first).
    await asyncio.sleep(3.5)

    # We expect at least 3 invocations
    count = int(marker.read_text())
    assert count >= 3, f"expected >=3 restart invocations, got {count}"

    # Stop it cleanly. stop() must cancel any pending restart from the
    # crashed-and-backing-off state too, not just kill a running child.
    await sup.stop("crasher")
    assert sup.children["crasher"].status == "stopped"
    # Verify the restart loop really stopped: count should not grow further.
    count_at_stop = int(marker.read_text())
    await asyncio.sleep(1.5)
    assert int(marker.read_text()) == count_at_stop, "restart loop did not stop"


@pytest.mark.asyncio
async def test_transient_does_not_restart_on_clean_exit(tmp_logs):
    spec = _spec("clean_exit", tmp_logs, py="pass", restart="transient")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    await sup.spawn("clean_exit", {})
    await asyncio.sleep(0.5)
    # Should be stopped (not running, not crashed, not restarting).
    state = sup.children["clean_exit"].status
    assert state == "stopped", f"expected stopped, got {state}"


@pytest.mark.asyncio
async def test_temporary_never_restarts_even_on_crash(tmp_logs):
    spec = _spec("oneshot_crash", tmp_logs, py="import sys; sys.exit(1)",
                 restart="temporary")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    await sup.spawn("oneshot_crash", {})
    await asyncio.sleep(0.5)
    state = sup.children["oneshot_crash"].status
    assert state == "crashed", f"expected crashed (no restart), got {state}"


@pytest.mark.asyncio
async def test_restart_budget_exhausts(tmp_logs):
    """A child that crashes repeatedly hits the restart cap and is marked failed."""
    spec = _spec("explode", tmp_logs, py="import sys; sys.exit(2)",
                 restart="permanent", max_restarts=2, restart_window_s=10.0)
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    await sup.spawn("explode", {})
    # Wait for the budget to exhaust. Backoff is 0.5*n with n in [1,2], so
    # restarts at ~0, 0.5, 1.5; budget exhausts on restart #3.
    await asyncio.sleep(3.0)
    state = sup.children["explode"].status
    assert state == "failed", f"expected failed, got {state}"


@pytest.mark.asyncio
async def test_reconcile_spawns_missing(tmp_logs):
    spec_a = _spec("alpha", tmp_logs, py="import time; time.sleep(60)")
    spec_b = _spec("beta", tmp_logs, py="import time; time.sleep(60)")

    def resolver(nid, m):
        return {"alpha": spec_a, "beta": spec_b}.get(nid)

    sup = Supervisor(runner_resolver=resolver, log_dir=tmp_logs)
    res = await sup.reconcile({"alpha": {}, "beta": {}})
    assert sorted(res["actions"]["spawned"]) == ["alpha", "beta"]
    assert sup.children["alpha"].status == "running"
    assert sup.children["beta"].status == "running"

    await sup.shutdown_all()


@pytest.mark.asyncio
async def test_reconcile_stops_extras(tmp_logs):
    spec = _spec("delete_me", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    # Spawn it...
    await sup.spawn("delete_me", {})
    # ...then reconcile against an empty manifest
    res = await sup.reconcile({})
    assert "delete_me" in res["actions"]["stopped"]
    assert sup.children["delete_me"].status == "stopped"


@pytest.mark.asyncio
async def test_reconcile_keeps_existing(tmp_logs):
    spec = _spec("kept", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)
    await sup.spawn("kept", {})
    pid_before = sup.children["kept"].pid

    res = await sup.reconcile({"kept": {}})
    assert "kept" in res["actions"]["kept"]
    assert sup.children["kept"].pid == pid_before  # not respawned

    await sup.stop("kept")


@pytest.mark.asyncio
async def test_list_processes_includes_started_at_and_uptime(tmp_logs):
    spec = _spec("introspect_me", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    await sup.spawn("introspect_me", {})
    await asyncio.sleep(0.3)

    procs = sup.list_processes()
    assert len(procs) == 1
    p = procs[0]
    assert p["node_id"] == "introspect_me"
    assert p["status"] == "running"
    assert p["uptime_seconds"] > 0
    assert p["pid"] > 0

    await sup.stop("introspect_me")


@pytest.mark.asyncio
async def test_event_emission(tmp_logs):
    """on_event callback receives spawn / stop / reconcile events."""
    events = []

    async def collect(evt):
        events.append(evt)

    spec = _spec("event_test", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs,
                     on_event=collect)

    await sup.spawn("event_test", {})
    await sup.stop("event_test")
    await asyncio.sleep(0.1)

    kinds = [e["kind"] for e in events]
    assert "spawn" in kinds
    assert "stop" in kinds
    assert any(e.get("node_id") == "event_test" for e in events)


@pytest.mark.asyncio
async def test_log_file_written(tmp_logs):
    log_dir = pathlib.Path(tmp_logs)
    log_dir.mkdir(parents=True, exist_ok=True)
    spec = _spec("loud", tmp_logs,
                 py="print('hello from child', flush=True); import time; time.sleep(0.3)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    await sup.spawn("loud", {})
    await asyncio.sleep(0.8)

    log_path = log_dir / "loud.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "hello from child" in content
    assert "supervisor start" in content


@pytest.mark.asyncio
async def test_shutdown_all_kills_children(tmp_logs):
    spec_a = _spec("aa", tmp_logs, py="import time; time.sleep(60)")
    spec_b = _spec("bb", tmp_logs, py="import time; time.sleep(60)")

    def resolver(nid, m):
        return {"aa": spec_a, "bb": spec_b}.get(nid)

    sup = Supervisor(runner_resolver=resolver, log_dir=tmp_logs)
    await sup.spawn("aa", {})
    await sup.spawn("bb", {})
    pid_a = sup.children["aa"].pid
    pid_b = sup.children["bb"].pid

    await sup.shutdown_all()
    await asyncio.sleep(0.2)

    for p in (pid_a, pid_b):
        with pytest.raises(ProcessLookupError):
            os.kill(p, 0)
