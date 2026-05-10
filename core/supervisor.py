"""
core/supervisor.py — Process supervisor for RAVEN_MESH nodes.

Responsibility: own the lifecycle of node processes declared in the manifest.

The mesh has two layers of state:
  1. DECLARATION (what `core.py` already manages): which nodes exist, their
     surfaces, edges, secrets. Loaded from the manifest yaml.
  2. PROCESSES (this module): actual OS processes running each node. Spawn,
     monitor, restart on crash, hot-add new ones, hot-remove old ones.

Until tonight, layer 2 was the user's job (`scripts/run_mesh.sh`). The
supervisor moves it into Core so editing the manifest can actually start and
stop processes.

Design notes (Python is not BEAM, but we copy the parts we can):
  - One ChildSpec per node_id derived from manifest. The spec contains the
    command, env, log path, and restart policy.
  - One ChildState per running child holds the asyncio.subprocess.Process
    and bookkeeping.
  - Each child has an associated Task that .wait()s on the process and
    handles restart on unexpected exit. (BEAM-style permanent/transient/
    temporary restart strategies, simplified.)
  - reconcile() takes the desired set of node_ids (from manifest) and the
    actual set (from running children) and computes the diff: spawn missing,
    stop extras, leave matching ones alone.
  - The supervisor does NOT decide HOW to start a node. It defers to a
    `runner_resolver` callable that maps node_id -> (cmd, env, cwd). For
    the existing mesh that resolver finds `scripts/run_<node_id>.sh`. For
    future mesh shapes the resolver could check a "runtime" field in the
    manifest (e.g. docker-image vs. local-process).

Why this is a prototype, not the final answer:
  - Python's subprocess + asyncio model has sharp edges (zombie reaping on
    SIGCHLD, signal masks across forks, log fd limits). BEAM solves all of
    these by being process-native.
  - Restart strategies here are basic. OTP's intensity/period throttling and
    one_for_all/rest_for_one strategies are not implemented.
  - We hold the manifest in memory and reconcile on demand. A real
    supervisor reacts to crashes via SIGCHLD; here we poll via subprocess
    .wait() in a per-child task.

Wire: `Supervisor` is owned by `CoreState`. New admin endpoints
  POST /v0/admin/spawn        body: {"node_id": "..."}
  POST /v0/admin/stop         body: {"node_id": "...", "graceful": true}
  POST /v0/admin/restart      body: {"node_id": "..."}
  POST /v0/admin/reconcile    body: {} — diff manifest vs. running, act
  GET  /v0/admin/processes    list of children with pid, status, uptime, restarts

The dashboard can wire a "Save & Reload" button that POSTs the new manifest
to /v0/admin/manifest then POSTs /v0/admin/reconcile. Two calls, real
hot-add / hot-remove.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import signal
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

log = logging.getLogger("supervisor")


# ---------- specs and state ----------

@dataclass
class ChildSpec:
    """Static description of how to start a child."""
    node_id: str
    cmd: list[str]
    env: dict[str, str]
    cwd: str
    log_path: str
    # 'permanent' = always restart, 'transient' = restart only on abnormal exit,
    # 'temporary' = never restart automatically.
    restart: str = "permanent"
    # Restart throttle: max N restarts in window seconds. Beyond that, the
    # supervisor gives up on the child and marks it 'failed'. Reset by a
    # successful long-lived run (uptime > window).
    max_restarts: int = 5
    restart_window_s: float = 60.0


@dataclass
class ChildState:
    spec: ChildSpec
    proc: Optional[asyncio.subprocess.Process] = None
    pid: Optional[int] = None
    started_at: float = 0.0
    last_exit_code: Optional[int] = None
    last_exit_at: float = 0.0
    restart_count: int = 0
    restart_window_start: float = 0.0
    status: str = "stopped"  # stopped|starting|running|crashed|failed|stopping
    monitor_task: Optional[asyncio.Task] = None
    log_fd = None  # file handle, kept open for the child's life

    def to_dict(self) -> dict:
        uptime = (time.time() - self.started_at) if self.status == "running" else 0.0
        return {
            "node_id": self.spec.node_id,
            "pid": self.pid,
            "status": self.status,
            "uptime_seconds": round(uptime, 1),
            "started_at": self.started_at,
            "last_exit_code": self.last_exit_code,
            "last_exit_at": self.last_exit_at,
            "restart_count": self.restart_count,
            "log_path": self.spec.log_path,
            "restart_policy": self.spec.restart,
            "cmd": self.spec.cmd,
        }


# ---------- supervisor ----------

# A runner_resolver maps node_id -> ChildSpec (or None if we don't know how to
# start that node — e.g. dummy one-shots). Centralizing this keeps the
# supervisor agnostic of the existing scripts/ layout.
RunnerResolver = Callable[[str, dict], Optional[ChildSpec]]


class Supervisor:
    def __init__(
        self,
        runner_resolver: RunnerResolver,
        log_dir: str = ".logs",
        on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        self.runner_resolver = runner_resolver
        self.log_dir = pathlib.Path(log_dir).resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.children: dict[str, ChildState] = {}
        self.lock = asyncio.Lock()
        self._stopping = False
        self._on_event = on_event

    async def _emit(self, kind: str, **fields) -> None:
        if not self._on_event:
            return
        try:
            await self._on_event({"ts": time.time(), "kind": kind, **fields})
        except Exception as e:  # pragma: no cover
            log.warning("supervisor on_event failed: %s", e)

    # ---- public API used by admin endpoints ----

    async def spawn(self, node_id: str, manifest_node: dict) -> dict:
        """Start a child. No-op (with warning) if already running."""
        async with self.lock:
            existing = self.children.get(node_id)
            if existing and existing.status in ("running", "starting"):
                return {
                    "ok": True,
                    "already_running": True,
                    "child": existing.to_dict(),
                }
            spec = self.runner_resolver(node_id, manifest_node)
            if spec is None:
                return {
                    "ok": False,
                    "error": "no_runner",
                    "details": f"no runner registered for node_id={node_id}",
                }
            return await self._start_locked(spec)

    async def stop(self, node_id: str, graceful: bool = True, timeout: float = 5.0) -> dict:
        async with self.lock:
            child = self.children.get(node_id)
            if not child:
                return {"ok": False, "error": "unknown_node"}
            # If the child is mid-backoff after a crash, cancel the pending
            # restart and mark it stopped. This is not "already stopped" — the
            # monitor task is alive and would respawn if we didn't intervene.
            if child.status == "crashed":
                if child.monitor_task and not child.monitor_task.done():
                    child.monitor_task.cancel()
                child.status = "stopped"
                return {"ok": True, "cancelled_pending_restart": True,
                        "child": child.to_dict()}
            if child.status in ("stopped", "failed"):
                return {"ok": True, "already_stopped": True, "child": child.to_dict()}
            return await self._stop_locked(child, graceful=graceful, timeout=timeout)

    async def restart(self, node_id: str, manifest_node: dict) -> dict:
        async with self.lock:
            child = self.children.get(node_id)
            if child and child.status in ("running", "starting"):
                await self._stop_locked(child, graceful=True, timeout=5.0)
            spec = self.runner_resolver(node_id, manifest_node)
            if spec is None:
                return {"ok": False, "error": "no_runner"}
            return await self._start_locked(spec)

    async def reconcile(self, manifest_nodes: dict[str, dict]) -> dict:
        """Diff manifest desired set vs. running set. Spawn missing, stop extras."""
        actions = {"spawned": [], "stopped": [], "skipped": [], "kept": [], "errors": []}
        desired_ids = set(manifest_nodes.keys())
        async with self.lock:
            running_ids = {nid for nid, c in self.children.items()
                           if c.status in ("running", "starting")}
            to_spawn = desired_ids - running_ids
            to_stop = running_ids - desired_ids
            to_keep = desired_ids & running_ids

            for nid in to_keep:
                actions["kept"].append(nid)

            for nid in to_spawn:
                spec = self.runner_resolver(nid, manifest_nodes[nid])
                if spec is None:
                    actions["skipped"].append({"node_id": nid, "reason": "no_runner"})
                    continue
                try:
                    res = await self._start_locked(spec)
                    if res.get("ok"):
                        actions["spawned"].append(nid)
                    else:
                        actions["errors"].append({"node_id": nid, "error": res})
                except Exception as e:
                    actions["errors"].append({"node_id": nid, "error": str(e)})

            for nid in to_stop:
                child = self.children.get(nid)
                if not child:
                    continue
                try:
                    await self._stop_locked(child, graceful=True, timeout=5.0)
                    actions["stopped"].append(nid)
                except Exception as e:
                    actions["errors"].append({"node_id": nid, "error": str(e)})

        await self._emit("reconcile", actions=actions)
        return {"ok": True, "actions": actions}

    def list_processes(self) -> list[dict]:
        return [c.to_dict() for c in self.children.values()]

    async def shutdown_all(self, timeout: float = 5.0) -> None:
        self._stopping = True
        async with self.lock:
            for child in list(self.children.values()):
                if child.status in ("running", "starting"):
                    try:
                        await self._stop_locked(child, graceful=True, timeout=timeout)
                    except Exception as e:
                        log.warning("error stopping %s: %s", child.spec.node_id, e)

    # ---- internals (caller holds self.lock) ----

    async def _start_locked(self, spec: ChildSpec) -> dict:
        # Open log file in append mode; supervisor never rotates these
        # (let the host's logrotate do it if needed).
        log_path = pathlib.Path(spec.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = open(log_path, "ab", buffering=0)
        log_fd.write(f"\n=== supervisor start at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())

        # Inherit current env, overlay spec env. Subprocess sees both.
        full_env = dict(os.environ)
        full_env.update(spec.env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.cmd,
                cwd=spec.cwd,
                env=full_env,
                stdout=log_fd,
                stderr=log_fd,
                # Detach from controlling terminal so SIGINT to core doesn't
                # cascade. The supervisor explicitly kills children on shutdown.
                start_new_session=True,
            )
        except FileNotFoundError as e:
            log_fd.close()
            return {"ok": False, "error": "exec_failed", "details": str(e)}
        except Exception as e:
            log_fd.close()
            return {"ok": False, "error": "spawn_failed", "details": str(e)}

        child = self.children.get(spec.node_id)
        if child is None:
            child = ChildState(spec=spec)
            self.children[spec.node_id] = child
        else:
            child.spec = spec  # respec on restart in case env changed

        child.proc = proc
        child.pid = proc.pid
        child.started_at = time.time()
        child.status = "running"
        child.log_fd = log_fd
        child.monitor_task = asyncio.create_task(self._monitor(child))

        await self._emit("spawn", node_id=spec.node_id, pid=proc.pid)
        log.info("[supervisor] spawned %s pid=%d log=%s", spec.node_id, proc.pid, spec.log_path)
        return {"ok": True, "child": child.to_dict()}

    async def _stop_locked(self, child: ChildState, *, graceful: bool, timeout: float) -> dict:
        child.status = "stopping"
        proc = child.proc
        if proc is None or proc.returncode is not None:
            child.status = "stopped"
            return {"ok": True, "child": child.to_dict()}
        # Cancel monitor first so it doesn't try to restart on the deliberate exit.
        if child.monitor_task and not child.monitor_task.done():
            child.monitor_task.cancel()
        try:
            if graceful:
                # SIGTERM to the whole process group (start_new_session=True
                # made the child a session leader; PGID==PID).
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    log.warning("[supervisor] %s ignored SIGTERM, sending SIGKILL", child.spec.node_id)
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        proc.kill()
                    await proc.wait()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                await proc.wait()
        finally:
            if child.log_fd:
                try:
                    child.log_fd.write(
                        f"=== supervisor stop at {time.strftime('%Y-%m-%d %H:%M:%S')} (rc={proc.returncode}) ===\n".encode()
                    )
                    child.log_fd.close()
                except Exception:
                    pass
                child.log_fd = None
            child.last_exit_code = proc.returncode
            child.last_exit_at = time.time()
            child.status = "stopped"
            child.proc = None
            child.pid = None

        await self._emit("stop", node_id=child.spec.node_id,
                         exit_code=child.last_exit_code)
        return {"ok": True, "child": child.to_dict()}

    async def _monitor(self, child: ChildState) -> None:
        """Wait for the child to exit. On unexpected exit, maybe restart."""
        proc = child.proc
        assert proc is not None
        try:
            rc = await proc.wait()
        except asyncio.CancelledError:
            return  # deliberate stop
        # Reached only on natural exit (crash or clean close)
        child.last_exit_code = rc
        child.last_exit_at = time.time()
        if child.log_fd:
            try:
                child.log_fd.write(
                    f"=== child exited rc={rc} at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
                )
                child.log_fd.close()
            except Exception:
                pass
            child.log_fd = None

        if self._stopping:
            child.status = "stopped"
            return

        # Decide whether to restart.
        spec = child.spec
        normal_exit = (rc == 0)
        if spec.restart == "temporary":
            should_restart = False
        elif spec.restart == "transient":
            should_restart = not normal_exit
        else:  # permanent
            should_restart = True

        if not should_restart:
            child.status = "stopped" if normal_exit else "crashed"
            await self._emit("exit", node_id=spec.node_id, rc=rc, restarted=False)
            return

        # Throttle: count restarts in a sliding window
        now = time.time()
        if (now - child.restart_window_start) > spec.restart_window_s:
            child.restart_window_start = now
            child.restart_count = 0
        child.restart_count += 1
        if child.restart_count > spec.max_restarts:
            child.status = "failed"
            log.error(
                "[supervisor] %s exceeded restart budget (%d/%ds), giving up",
                spec.node_id, spec.max_restarts, spec.restart_window_s,
            )
            await self._emit("give_up", node_id=spec.node_id,
                             restart_count=child.restart_count)
            return

        # Backoff: linear on restart_count, capped at 10s
        backoff_s = min(0.5 * child.restart_count, 10.0)
        log.warning("[supervisor] %s exited rc=%d, restarting in %.1fs (attempt %d/%d)",
                    spec.node_id, rc, backoff_s, child.restart_count, spec.max_restarts)
        await self._emit("restart_scheduled", node_id=spec.node_id,
                         rc=rc, attempt=child.restart_count, backoff_s=backoff_s)
        child.status = "crashed"
        await asyncio.sleep(backoff_s)

        # Re-acquire the lock to actually restart.
        async with self.lock:
            if self._stopping:
                return
            # If something else (admin stop, reconcile-remove) already handled it,
            # don't auto-restart.
            current = self.children.get(spec.node_id)
            if current is not child:
                return
            if current.status in ("stopped", "stopping"):
                return
            await self._start_locked(spec)


# ---------- default runner resolver: scripts/run_<node_id>.sh ----------

def make_script_resolver(repo_root: str, log_dir: str) -> RunnerResolver:
    """Resolver that finds scripts/run_<node_id>.sh in repo_root.

    Returns None for nodes without a script (e.g. dummy actors that are
    one-shot envelope senders, not long-running).
    """
    root = pathlib.Path(repo_root).resolve()
    logs = pathlib.Path(log_dir).resolve()

    def resolve(node_id: str, manifest_node: dict) -> Optional[ChildSpec]:
        # Allow manifest to override per-node command via metadata.runner.cmd
        meta = manifest_node.get("metadata", {}) if manifest_node else {}
        runner_meta = meta.get("runner", {}) if isinstance(meta, dict) else {}
        if "cmd" in runner_meta:
            cmd = runner_meta["cmd"]
            if isinstance(cmd, str):
                cmd = ["/bin/sh", "-c", cmd]
        else:
            script = root / "scripts" / f"run_{node_id}.sh"
            if not script.exists():
                return None
            cmd = ["/bin/bash", str(script)]
        env = dict(runner_meta.get("env", {}) or {})
        # Pass MESH_NODE_ID for any script that wants to introspect.
        env["MESH_NODE_ID"] = node_id
        return ChildSpec(
            node_id=node_id,
            cmd=cmd,
            env=env,
            cwd=str(root),
            log_path=str(logs / f"{node_id}.log"),
            restart=runner_meta.get("restart", "permanent"),
            max_restarts=int(runner_meta.get("max_restarts", 5)),
            restart_window_s=float(runner_meta.get("restart_window_s", 60.0)),
        )

    return resolve
