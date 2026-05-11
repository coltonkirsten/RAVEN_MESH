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


# ---------------------------------------------------------------------------
# Wave-2 additions: on_demand, graceful drain, exponential backoff, metrics.
# All four are PROTOCOL-LAYER (generic ChildSpec/ChildState mechanics) — no
# node-specific knowledge belongs in the supervisor.
# ---------------------------------------------------------------------------


# ---- exponential backoff ----

def test_backoff_schedule_is_exponential_capped_at_30():
    from core.supervisor import _backoff_seconds, _BACKOFF_CAP_S
    assert _backoff_seconds(1) == 0.5
    assert _backoff_seconds(2) == 1.0
    assert _backoff_seconds(3) == 2.0
    assert _backoff_seconds(4) == 4.0
    assert _backoff_seconds(5) == 8.0
    assert _backoff_seconds(6) == 16.0
    assert _backoff_seconds(7) == _BACKOFF_CAP_S  # 32 > 30 → capped
    assert _backoff_seconds(20) == _BACKOFF_CAP_S
    assert _BACKOFF_CAP_S == 30.0


# ---- on_demand restart strategy ----

@pytest.mark.asyncio
async def test_on_demand_not_spawned_during_reconcile(tmp_logs):
    """on_demand children are deferred, not spawned, by reconcile()."""
    spec = _spec("lazy", tmp_logs, py="import time; time.sleep(60)",
                 restart="on_demand")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    res = await sup.reconcile({"lazy": {}})
    assert "lazy" in res["actions"]["deferred"]
    assert "lazy" not in res["actions"]["spawned"]
    # Recorded but not running
    child = sup.children.get("lazy")
    assert child is not None
    assert child.status == "stopped"
    assert child.pid is None


@pytest.mark.asyncio
async def test_ensure_running_wakes_on_demand_child(tmp_logs):
    """ensure_running() spawns the child on first call; second call is a no-op."""
    spec = _spec("waker", tmp_logs, py="import time; time.sleep(60)",
                 restart="on_demand")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    res1 = await sup.ensure_running("waker", {})
    assert res1["ok"]
    assert sup.children["waker"].status == "running"
    pid1 = sup.children["waker"].pid
    assert pid1 > 0

    res2 = await sup.ensure_running("waker", {})
    assert res2["ok"]
    assert res2.get("already_running") is True
    assert sup.children["waker"].pid == pid1

    await sup.stop("waker")


@pytest.mark.asyncio
async def test_on_demand_idle_shutdown_then_respawn(tmp_logs):
    """After idle_shutdown_s of silence, the child is reaped; ensure_running respawns."""
    log_path = pathlib.Path(tmp_logs) / "idle.log"
    spec = ChildSpec(
        node_id="idle",
        cmd=[sys.executable, "-c", "import time; time.sleep(60)"],
        env={},
        cwd=str(pathlib.Path.cwd()),
        log_path=str(log_path),
        restart="on_demand",
        idle_shutdown_s=0.6,  # short for test
    )
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    await sup.ensure_running("idle", {})
    pid1 = sup.children["idle"].pid
    assert sup.children["idle"].status == "running"

    # Wait past idle window (poll = idle/4 = 0.15s; trips at >=0.6s)
    await asyncio.sleep(1.2)

    assert sup.children["idle"].status == "stopped"
    with pytest.raises(ProcessLookupError):
        os.kill(pid1, 0)

    # ensure_running again wakes a fresh process
    res = await sup.ensure_running("idle", {})
    assert res["ok"]
    pid2 = sup.children["idle"].pid
    assert pid2 != pid1
    assert sup.children["idle"].status == "running"

    await sup.stop("idle")


@pytest.mark.asyncio
async def test_on_demand_natural_exit_does_not_restart(tmp_logs):
    """on_demand child that exits on its own is NOT restarted."""
    spec = _spec("oneshot_demand", tmp_logs, py="pass", restart="on_demand")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    await sup.ensure_running("oneshot_demand", {})
    await asyncio.sleep(0.6)

    child = sup.children["oneshot_demand"]
    assert child.status == "stopped"
    assert child.pid is None
    assert child.total_restart_count == 0


@pytest.mark.asyncio
async def test_begin_work_touches_activity_and_resets_idle(tmp_logs):
    """begin_work() bumps last_activity_at so a busy child isn't reaped."""
    log_path = pathlib.Path(tmp_logs) / "busy.log"
    spec = ChildSpec(
        node_id="busy",
        cmd=[sys.executable, "-c", "import time; time.sleep(60)"],
        env={},
        cwd=str(pathlib.Path.cwd()),
        log_path=str(log_path),
        restart="on_demand",
        idle_shutdown_s=0.6,
    )
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    await sup.ensure_running("busy", {})
    pid = sup.children["busy"].pid
    # Keep tickling the child every 200ms for a full second — should outlive
    # the 600ms idle window because each begin_work resets the clock.
    for _ in range(5):
        await asyncio.sleep(0.2)
        sup.begin_work("busy")
        sup.end_work("busy")
    assert sup.children["busy"].status == "running"
    assert sup.children["busy"].pid == pid

    await sup.stop("busy")


# ---- graceful drain ----

@pytest.mark.asyncio
async def test_can_accept_reflects_child_state(tmp_logs):
    spec = _spec("acceptor", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    # Unknown child: caller decides (returns True so caller isn't blocked
    # on supervisor knowledge).
    assert sup.can_accept("unknown") is True

    await sup.spawn("acceptor", {})
    assert sup.can_accept("acceptor") is True

    await sup.stop("acceptor")
    assert sup.can_accept("acceptor") is False


@pytest.mark.asyncio
async def test_drain_finishes_in_flight_then_stops(tmp_logs):
    """drain() flips to draining, waits for in_flight==0, then SIGTERMs."""
    spec = _spec("drainable", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)
    await sup.spawn("drainable", {})

    # Pretend dispatcher started 2 envelopes
    assert sup.begin_work("drainable") is True
    assert sup.begin_work("drainable") is True
    assert sup.children["drainable"].in_flight == 2

    # Kick off drain in the background
    drain_task = asyncio.create_task(sup.drain("drainable", timeout=5.0))
    await asyncio.sleep(0.05)

    # Now in draining: new work refused, can_accept False
    assert sup.children["drainable"].status == "draining"
    assert sup.can_accept("drainable") is False
    assert sup.begin_work("drainable") is False

    # Finish the in-flight work
    sup.end_work("drainable")
    await asyncio.sleep(0.05)
    assert sup.children["drainable"].status == "draining"  # still 1 left
    sup.end_work("drainable")

    res = await drain_task
    assert res["ok"] is True
    assert res["timed_out"] is False
    assert res["drained_in_flight"] == 2
    assert sup.children["drainable"].status == "stopped"


@pytest.mark.asyncio
async def test_drain_with_no_in_flight_completes_immediately(tmp_logs):
    spec = _spec("idle_drain", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)
    await sup.spawn("idle_drain", {})

    res = await sup.drain("idle_drain", timeout=5.0)
    assert res["ok"] is True
    assert res["timed_out"] is False
    assert res["drained_in_flight"] == 0
    assert sup.children["idle_drain"].status == "stopped"


@pytest.mark.asyncio
async def test_drain_times_out_and_kills_anyway(tmp_logs):
    """If in_flight never hits 0, drain still stops the child after timeout."""
    spec = _spec("hangs", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)
    await sup.spawn("hangs", {})

    # Pretend in-flight work that nobody finishes
    sup.begin_work("hangs")

    res = await sup.drain("hangs", timeout=0.3)
    assert res["ok"] is True
    assert res["timed_out"] is True
    assert sup.children["hangs"].status == "stopped"


@pytest.mark.asyncio
async def test_drain_unknown_node_fails(tmp_logs):
    sup = Supervisor(runner_resolver=lambda nid, m: None, log_dir=tmp_logs)
    res = await sup.drain("ghost", timeout=1.0)
    assert res["ok"] is False
    assert res["error"] == "unknown_node"


# ---- metrics ----

@pytest.mark.asyncio
async def test_metrics_includes_uptime_and_zero_restarts(tmp_logs):
    spec = _spec("metrified", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)
    await sup.spawn("metrified", {})
    await asyncio.sleep(0.2)

    m = sup.metrics()
    assert m["totals"]["children"] == 1
    assert m["totals"]["running"] == 1
    assert m["totals"]["restarts"] == 0
    assert m["supervisor_uptime_seconds"] >= 0

    children = m["children"]
    assert len(children) == 1
    c = children[0]
    assert c["node_id"] == "metrified"
    assert c["status"] == "running"
    assert c["uptime_seconds"] > 0
    assert c["restart_count_total"] == 0
    assert c["in_flight"] == 0

    await sup.stop("metrified")


@pytest.mark.asyncio
async def test_metrics_counts_restarts_across_crashes(tmp_logs):
    """total_restart_count accumulates across crash-restart cycles."""
    spec = _spec("crashy", tmp_logs, py="import sys; sys.exit(1)",
                 restart="permanent", max_restarts=10, restart_window_s=30.0)
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)
    await sup.spawn("crashy", {})

    # Wait for at least 2 restart cycles. Exponential backoff: 0.5s, 1.0s.
    await asyncio.sleep(2.0)
    await sup.stop("crashy")

    m = sup.metrics()
    assert m["totals"]["restarts"] >= 2
    c = next(c for c in m["children"] if c["node_id"] == "crashy")
    assert c["restart_count_total"] >= 2


@pytest.mark.asyncio
async def test_metrics_tracks_in_flight_during_drain(tmp_logs):
    spec = _spec("inflight", tmp_logs, py="import time; time.sleep(60)")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)
    await sup.spawn("inflight", {})
    sup.begin_work("inflight")
    sup.begin_work("inflight")

    m = sup.metrics()
    c = next(c for c in m["children"] if c["node_id"] == "inflight")
    assert c["in_flight"] == 2

    sup.end_work("inflight")
    sup.end_work("inflight")
    await sup.stop("inflight")


@pytest.mark.asyncio
async def test_metrics_classifies_on_demand_warm(tmp_logs):
    """on_demand children running register as on_demand_warm in metrics."""
    spec = _spec("warm", tmp_logs, py="import time; time.sleep(60)",
                 restart="on_demand")
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs)

    # Nothing warm yet
    m = sup.metrics()
    assert m["totals"]["on_demand_warm"] == 0

    await sup.ensure_running("warm", {})
    m = sup.metrics()
    assert m["totals"]["on_demand_warm"] == 1
    assert m["totals"]["running"] == 1

    await sup.stop("warm")


# ---- exponential backoff: end-to-end via crash schedule ----

@pytest.mark.asyncio
async def test_exponential_backoff_visible_in_emitted_events(tmp_logs):
    """Emitted restart_scheduled events carry the new exponential backoff_s."""
    events = []

    async def collect(evt):
        events.append(evt)

    spec = _spec("backoffy", tmp_logs, py="import sys; sys.exit(1)",
                 restart="permanent", max_restarts=10, restart_window_s=30.0)
    sup = Supervisor(runner_resolver=lambda nid, m: spec, log_dir=tmp_logs,
                     on_event=collect)

    await sup.spawn("backoffy", {})
    # Wait long enough to see a few restart_scheduled events (0.5 + 1.0 = 1.5s).
    await asyncio.sleep(2.0)
    await sup.stop("backoffy")

    backoffs = [e["backoff_s"] for e in events if e["kind"] == "restart_scheduled"]
    assert len(backoffs) >= 2
    # First two attempts: 0.5, 1.0 (exponential, not 0.5 + 0.5*n linear)
    assert backoffs[0] == 0.5
    assert backoffs[1] == 1.0


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


# ---------------------------------------------------------------------------
# Default runner resolver — manifest-driven command resolution.
# ---------------------------------------------------------------------------


def test_default_resolver_reads_metadata_runner_cmd_string(tmp_path, tmp_logs):
    """metadata.runner.cmd as a string is wrapped in /bin/sh -c."""
    from core.supervisor import make_script_resolver
    resolve = make_script_resolver(str(tmp_path), tmp_logs)
    spec = resolve("alpha", {"metadata": {"runner": {"cmd": "echo hi"}}})
    assert spec is not None
    assert spec.cmd == ["/bin/sh", "-c", "echo hi"]
    assert spec.env["MESH_NODE_ID"] == "alpha"


def test_default_resolver_reads_metadata_runner_cmd_list(tmp_path, tmp_logs):
    """metadata.runner.cmd as a list is passed through verbatim."""
    from core.supervisor import make_script_resolver
    resolve = make_script_resolver(str(tmp_path), tmp_logs)
    spec = resolve("beta", {"metadata": {"runner": {"cmd": ["/usr/bin/env", "true"]}}})
    assert spec is not None
    assert spec.cmd == ["/usr/bin/env", "true"]


def test_default_resolver_no_scripts_dir_no_cmd_returns_none(tmp_path, tmp_logs):
    """With no scripts/ dir and no metadata.runner.cmd, the resolver returns None.

    This is the post-strip default: nothing is implied by the filesystem.
    """
    from core.supervisor import make_script_resolver
    # tmp_path is a fresh dir with no scripts/ subdir.
    assert not (tmp_path / "scripts").exists()
    resolve = make_script_resolver(str(tmp_path), tmp_logs)
    assert resolve("gamma", {}) is None
    assert resolve("gamma", {"metadata": {}}) is None


def test_default_resolver_falls_back_to_scripts_dir_when_present(tmp_path, tmp_logs):
    """Legacy fallback: if scripts/ exists, scripts/run_<id>.sh is honoured."""
    from core.supervisor import make_script_resolver
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "run_delta.sh"
    script.write_text("#!/bin/bash\necho ok\n")
    script.chmod(0o755)

    resolve = make_script_resolver(str(tmp_path), tmp_logs)
    spec = resolve("delta", {})
    assert spec is not None
    assert spec.cmd == ["/bin/bash", str(script)]
    # Missing script in an existing scripts/ dir still returns None.
    assert resolve("missing", {}) is None
