"""Spin up nats-server, the echo and kanban nodes, then run dashboard once.

Usage:
    .venv/bin/python run_demo.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import signal
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from nats_core import NatsBroker  # noqa: E402


async def main() -> int:
    work = HERE / "run_logs"
    work.mkdir(exist_ok=True)
    broker = NatsBroker(
        manifest_path=str(HERE / "manifest.yaml"),
        work_dir=str(work),
        port=int(os.environ.get("NATS_PORT", "4233")),
    )
    await broker.start()
    print(f"[run_demo] broker up (port {broker.port}); audit -> "
          f"{work / 'audit.jsonl'}", flush=True)

    py = sys.executable
    env = {**os.environ, "NATS_PORT": str(broker.port),
           "PYTHONUNBUFFERED": "1"}
    nodes = []
    for name in ("echo_node", "kanban_node"):
        log = open(work / f"{name}.log", "ab")
        p = subprocess.Popen(
            [py, str(HERE / "nodes" / f"{name}.py")],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        nodes.append((name, p, log))
    await asyncio.sleep(1.0)  # let them subscribe

    print("[run_demo] running dashboard ...", flush=True)
    dash = subprocess.run(
        [py, str(HERE / "nodes" / "dashboard_node.py")],
        env=env, capture_output=True, text=True, timeout=20,
    )
    print(dash.stdout)
    if dash.stderr:
        print("[dashboard stderr]", dash.stderr, file=sys.stderr)

    for name, p, log in nodes:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
    await broker.stop()
    print(f"[run_demo] done. logs in {work}", flush=True)
    return dash.returncode


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
