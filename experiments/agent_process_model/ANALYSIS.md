# Agent Process Model: Daemon vs Cold-Spawn

**Question.** Today every RAVEN_MESH node boots as a long-lived `python3` process under `core/supervisor.py`. Should agents instead be cold-spawned per envelope (serverless-style), like AWS Lambda functions, with the supervisor as the dispatcher?

**Short answer.** Most of the current mesh cannot be cold-spawned without losing observable behavior. But a clearly-bounded subset (pure tools, dummy actors, the `weather`-shaped surfaces we expect to add for capability nodes) gives up nothing and gains real benefits. The right move is a **hybrid supervisor** — keep `permanent`/`transient`/`temporary` for true daemons, add a fourth `on_demand` strategy for stateless surfaces — not a wholesale flip.

This document presents:

1. A classification of every node in `nodes/`.
2. Real benchmark numbers from a working spawn-per-invoke prototype (`spawn_runner.py`).
3. Where cold-spawn helps, where it hurts.
4. A recommendation per node.
5. A proposed `on_demand` restart strategy as an inline diff against `core/supervisor.py` (NOT applied — design only).

---

## 1. Classification table

Read from `nodes/*` source. "State" = the in-memory objects that today persist across invocations and would be lost on every spawn. "Cold-spawn safe?" answers: if Core forked a fresh interpreter for each envelope addressed to this surface, would the user-observable behavior change?

| Node | Class | State held in memory across invocations | Cold-spawn safe? |
|---|---|---|---|
| `dummy_actor` | **spawnable** | none — already a one-shot CLI that connects, fires one invocation, exits | yes (it already works this way) |
| `dummy_capability` | daemon today, **spawnable** in spirit | none — pure echo handler; only stays running because `node.serve()` blocks on SSE | yes (handler is stateless) |
| `dummy_approval` | daemon today, **spawnable** in spirit | none beyond a `DENY` env flag | yes |
| `dummy_hybrid` | daemon today, **spawnable** in spirit | none — both inbox and tool handlers are pure | yes |
| `webui_node` | **hybrid** | `state` dict (`message`, `color`, `updated_at`) + SSE `subscribers` set | no — browsers hold open SSE; `state` is the whole point |
| `kanban_node` | **hybrid** | `columns`, `cards` (disk-persisted, but cached in-process), `hub` SSEHub, `_lock`, `_ui_hidden` | no for SSE; tool surfaces themselves *could* be cold-spawned if disk is the source of truth |
| `approval_node` | **stateful daemon** | `pending` dict of (msg_id → asyncio.Future), SSE `subscribers`, aiohttp server :8803 | **no** — pending Futures are the load-bearing state; if process dies, every in-flight approval times out |
| `cron_node` | **stateful daemon** | `crons` dict + `tasks` dict of asyncio.Task runners that sleep until fire time | **no** — the asyncio.Tasks ARE the scheduler; cold-spawn = no scheduler |
| `human_node` | **stateful daemon** | `messages` history (cap 100), SSE `subscribers`, aiohttp :8802 | no — message history and live UI sockets are user-visible |
| `nexus_agent` | **stateful daemon** | `session_id`, `control_token`, `inspector` SSE state, `_lock`, persistent claude session resumes | **no** — session resumption + serialized claude runs require a single owner |
| `nexus_agent_isolated` | **stateful daemon** | same as nexus_agent + Docker volume for ledger | **no** — same reason; container lifecycle hangs off the long-lived parent |
| `voice_actor` | **stateful daemon** | OpenAI Realtime websocket session, `MicCapture`/`SpeakerPlayback` audio handles, three concurrent asyncio loops (`_mic_task`, `_evt_task`, `_meter_task`) | **emphatically no** — websocket + audio device + overlapped event streams cannot be cold-started per envelope |

Five of the eleven implementations are stateful daemons in the strict sense (state would be observably lost). Four are daemons by accident — they call `node.serve()` only because the SDK assumes a long-lived stream — and would lose nothing by switching to cold-spawn. Two are hybrid: their tool surfaces are stateless, but their UI surfaces require persistent SSE.

---

## 2. Benchmark: cold-spawn vs daemon

`spawn_runner.py` forks a fresh `python3 -m cold_handlers.<surface>` per envelope, pipes the JSON to its stdin, reads the response off stdout, and verifies the HMAC signature. `daemon_runner.py` runs ONE long-lived interpreter that loops on stdin, processing newline-delimited envelopes. Same handler module in both modes; only the process model differs.

`benchmark.py` ran 100 invocations of each (after 5 warmup discards) on this machine (`darwin`, 10 cores, Python 3.12.12). Results are persisted in `results/summary.json`.

| Surface | Mode | p50 | p95 | p99 | RSS |
|---|---|---|---|---|---|
| `echo.invoke` | cold-spawn | **40.68 ms** | 47.05 ms | 48.29 ms | ~18.9 MB while alive |
| `echo.invoke` | daemon | **0.040 ms** | 0.072 ms | 0.095 ms | 24.9 MB resident always |
| `weather.lookup` | cold-spawn | **40.70 ms** | 47.87 ms | 51.04 ms | ~19.2 MB while alive |
| `weather.lookup` | daemon | **0.043 ms** | 0.069 ms | 0.099 ms | 25.0 MB resident always |

**Cold-spawn is ~1000× slower at p50 and ~500× slower at p99.** The ~40 ms floor is essentially Python interpreter startup + module import + asyncio.run() overhead. The handler itself takes <1 ms; the rest is process plumbing. Memory tells the opposite story: the cold-spawn child is *transient* — it dies after the response — so steady-state idle cost is **0 KB**, while the daemon holds ~25 MB whether anything is happening or not.

The numbers reframe the question. Cold-spawn isn't competing with daemons on latency; it can't. It's competing on **idle cost** — and at six idle daemons holding 25 MB each, that's 150 MB of resident RAM doing nothing.

---

## 3. Where cold-spawn helps

- **Idle RAM goes to zero.** Eleven nodes × ~25 MB = ~275 MB resident even if no envelopes are flowing. Cold-spawn moves that to "0 MB at rest, 25 MB only while a request is in flight." On a laptop with the whole mesh + the dashboard + nexus_agent's web UI, that's real headroom.
- **Security isolation is per-invocation.** Today, if an exploit corrupts a node's process, the daemon stays compromised until restart. With cold-spawn, the blast radius is exactly one envelope — the next request gets a fresh interpreter with fresh memory.
- **Scaling is trivial.** N concurrent requests = N concurrent processes (bounded by `max_concurrent`), no lock contention inside one process. Today every node serializes work through its single asyncio loop.
- **No restart logic for stateless tools.** The supervisor's `permanent`/`transient`/restart-window throttling is plumbing that exists because long-lived processes crash. A cold-spawn handler's lifecycle is request → exit; there is nothing to restart.
- **Hot-swap without graceful drain.** Edit `cold_handlers/weather.py`, save, next envelope picks up the new code. No process to bounce.

## 4. Where cold-spawn hurts

- **40 ms latency floor, even for a no-op.** That floor is acceptable for most "tool call" surfaces but unacceptable for `voice_actor` (which expects ~100 ms end-to-end speech turns), `approval_node` (the SSE fan-out has to be live), and any path on an interactive critical path.
- **Cannot hold sessions.** `nexus_agent`'s session resumption, `voice_actor`'s OpenAI websocket, `human_node`'s SSE subscribers — these require the same process to be alive across calls. Cold-spawn breaks them.
- **Cannot subscribe to events.** Every node currently calls `node_sdk.serve()`, which opens an SSE stream from Core to receive `deliver` events. Cold-spawn nodes don't hold a stream; the supervisor must dispatch envelopes *into* them. That changes the wire model from push to pull and is incompatible with the existing `MeshNode` SDK.
- **No background timers, no schedulers.** `cron_node`'s entire reason to exist is asyncio Tasks that sleep until fire time. Cold-spawn = no scheduler.
- **Per-invocation cold caches.** Anything cached in module globals (DB connections, parsed YAML, model weights) is rebuilt on every call. Tolerable for `echo`/`weather`; intolerable for an LLM-loading handler.
- **Logging fragments.** A daemon's stderr is one continuous file the supervisor tails. Cold-spawn produces N short log files (or interleaved chunks) that are harder to follow during incident response.

---

## 5. Recommendation per node

| Node | Verdict | Rationale |
|---|---|---|
| `dummy_actor` | **switch to cold-spawn (already is)** | One-shot by construction. No SDK change. |
| `dummy_capability`, `dummy_approval`, `dummy_hybrid` | **switch to cold-spawn** | These are illustrative stubs; demonstrate the new model on something low-stakes. |
| `webui_node` (tool surfaces) | **hybrid** | `show_message`/`change_color` could be cold-spawned IF state moves to a tiny shared file; SSE remains a daemon. Probably not worth splitting — keep as daemon. |
| `kanban_node` (tool surfaces) | **keep daemon** | Disk-backed state means you *could* cold-spawn the tool calls, but the SSE hub broadcasts state changes live to the browser. Splitting is more code than it saves. |
| `approval_node` | **keep daemon** | Pending-approval Futures are the whole point. Cold-spawn would orphan in-flight approvals. |
| `cron_node` | **keep daemon** | Scheduler IS the in-memory state. |
| `human_node` | **keep daemon** | Live message history + UI sockets. |
| `nexus_agent`, `nexus_agent_isolated` | **keep daemon** | Session resumption, serialized claude runs, MCP bridge lifecycle. |
| `voice_actor` | **keep daemon — never cold-spawn** | Real-time websocket + audio device. |

Net change: 4 of 11 nodes move to cold-spawn. The rest stay daemons. The supervisor needs to learn one new lifecycle: `on_demand`.

---

## 6. Proposed `on_demand` strategy (design only — DO NOT APPLY)

Today `core/supervisor.py:82` defines `restart` as one of `permanent` / `transient` / `temporary`. We add a fourth: `on_demand`. The semantics:

- The child is NOT spawned at supervisor startup or `reconcile()`.
- The first envelope routed to one of the child's surfaces triggers a spawn.
- After the response, an idle timer starts. If no new envelope arrives within `idle_shutdown_s`, the supervisor sends SIGTERM and reaps the child.
- A subsequent envelope spawns it again.

Two implementation shapes:

**(a) Per-envelope cold-spawn** — every envelope = new process. Simplest, matches `spawn_runner.py`. Best for handlers that are truly stateless (echo, weather).

**(b) Spawn-and-keep-warm-for-N-seconds** — first envelope spawns; subsequent envelopes within `idle_shutdown_s` reuse the same process; idle timer expires → SIGTERM. Faster for bursty workloads (no 40 ms floor on the 2nd-Nth call), still drops to zero RAM during silence. This is closer to AWS Lambda's "warm container" behavior.

The `on_demand` strategy below uses (b), with `idle_shutdown_s` and `cold_spawn` as new `ChildSpec` fields. Falling back to (a) is just `idle_shutdown_s = 0`.

### Inline diff against `core/supervisor.py`

```diff
@@ class ChildSpec:
     restart: str = "permanent"
+    # New: idle window for restart="on_demand". After this many seconds
+    # without an envelope, the supervisor SIGTERMs the child and waits
+    # for the next envelope to spawn it again. 0 = always cold-spawn.
+    idle_shutdown_s: float = 30.0
     max_restarts: int = 5
     restart_window_s: float = 60.0
@@ class ChildState:
     status: str = "stopped"
     monitor_task: Optional[asyncio.Task] = None
     log_fd = None
+    # New: tracks the last envelope dispatched to this child. The idle
+    # reaper uses this to decide when to shut down an on_demand child.
+    last_envelope_at: float = 0.0
+    idle_reaper_task: Optional[asyncio.Task] = None
@@ class Supervisor:
+    async def ensure_running(self, node_id: str, manifest_node: dict) -> dict:
+        """Spawn the child if it's not running. Used by the dispatcher to
+        wake an on_demand child on the first envelope. Idempotent."""
+        async with self.lock:
+            child = self.children.get(node_id)
+            if child and child.status in ("running", "starting"):
+                child.last_envelope_at = time.time()
+                return {"ok": True, "child": child.to_dict()}
+            spec = self.runner_resolver(node_id, manifest_node)
+            if spec is None:
+                return {"ok": False, "error": "no_runner"}
+            res = await self._start_locked(spec)
+            new_child = self.children.get(node_id)
+            if new_child and spec.restart == "on_demand":
+                new_child.last_envelope_at = time.time()
+                new_child.idle_reaper_task = asyncio.create_task(
+                    self._idle_reaper(new_child)
+                )
+            return res
+
+    async def _idle_reaper(self, child: ChildState) -> None:
+        spec = child.spec
+        while True:
+            await asyncio.sleep(max(spec.idle_shutdown_s / 4, 1.0))
+            if child.status != "running":
+                return
+            idle_for = time.time() - child.last_envelope_at
+            if idle_for >= spec.idle_shutdown_s:
+                log.info("[supervisor] %s idle %.1fs, shutting down (on_demand)",
+                         spec.node_id, idle_for)
+                async with self.lock:
+                    if child.status == "running":
+                        await self._stop_locked(child, graceful=True, timeout=5.0)
+                return
@@ async def reconcile(self, manifest_nodes: dict[str, dict]) -> dict:
-            to_spawn = desired_ids - running_ids
+            # on_demand children are NOT spawned eagerly during reconcile.
+            on_demand_ids = {
+                nid for nid in desired_ids
+                if (self.runner_resolver(nid, manifest_nodes[nid]) or
+                    ChildSpec(node_id=nid, cmd=[], env={}, cwd="", log_path="")
+                   ).restart == "on_demand"
+            }
+            to_spawn = (desired_ids - running_ids) - on_demand_ids
@@ async def _monitor(self, child: ChildState) -> None:
-        elif spec.restart == "transient":
+        elif spec.restart == "on_demand":
+            # Natural exit of an on_demand child is normal — that's the
+            # whole point. Don't auto-restart; wait for the next envelope.
+            child.status = "stopped"
+            await self._emit("on_demand_exit", node_id=spec.node_id, rc=rc)
+            return
+        elif spec.restart == "transient":
             should_restart = not normal_exit
```

The dispatcher (caller of the wire protocol's `/v0/invoke`) needs one new line: before routing an envelope, call `supervisor.ensure_running(target_node_id, manifest_entry)`. That makes on_demand children indistinguishable from permanent ones to the rest of the mesh — they wake themselves up.

Two follow-ups intentionally left out: (1) eviction policy when total active on_demand processes exceeds a budget — for now relying on `max_concurrent` in the spawn runner; (2) batching multiple envelopes into one warm child window — the dispatcher could hold a small per-child queue and reuse the existing process via a stdin pipe, mirroring `daemon_runner.py`'s newline-delimited protocol. That second one is where cold-spawn's 40 ms floor goes away for bursty workloads, and it's the strongest argument for shape (b) over shape (a).

---

## 7. Files in this experiment

- `spawn_sdk.py` — minimal stdin/stdout envelope harness for cold-spawn handlers.
- `spawn_runner.py` — the dispatcher; forks a process per envelope, returns response.
- `cold_handlers/echo.py`, `cold_handlers/weather.py` — example cold-spawn surfaces.
- `daemon_runner.py` — long-lived counterpart; lets one process handle many envelopes (used for benchmark fairness).
- `benchmark.py` — runs 100 invocations of each, captures p50/p95/p99 + RSS.
- `results/echo_invoke.json`, `results/weather_lookup.json`, `results/summary.json` — raw data.

Run the benchmark yourself: `python3 benchmark.py` from this directory.
