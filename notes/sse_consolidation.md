# SSE consolidation — 2026-05-10

## Why

Eight independent copies of the same SSE serving loop existed across the
repo. Every node that exposes a `/events` stream had its own:

- `text/event-stream` header dance,
- per-connection `asyncio.Queue` registration with a manually-managed
  `set` on a `state`/`board` object,
- 15-second heartbeat fallback,
- `ConnectionResetError` / `BrokenPipeError` catches,
- `try/finally` to discard the queue on disconnect.

A bug fixed in one place (e.g. heartbeat-on-broken-pipe handling) was
guaranteed to leave the others broken. This change extracts the loop into
`node_sdk/sse.py`.

## Audit — all SSE implementations

| File | Lines | Event names | Replay |
| --- | --- | --- | --- |
| `core/core.py` | 364–445 (node stream), 473–520 (live logs) | `hello`, `deliver`, `heartbeat` / log events | n/a |
| `nodes/nexus_agent/web/server.py` | 123–154 | per-`kind` (publish-driven) | last 100 history events |
| `nodes/kanban_node/kanban_node.py` | 327–356 | `state` | one snapshot at connect |
| `nodes/approval_node/approval_node.py` | 108–133 | `state` | one snapshot at connect |
| `nodes/voice_actor/voice_actor.py` | 680ff | `state` / live transcript events | snapshot |
| `nodes/webui_node/webui_node.py` | 80ff | per-`kind` events | n/a |
| `nodes/human_node/human_node.py` | 125ff | per-`kind` events | n/a |
| `nodes/nexus_agent_isolated/web/server.py` | 90ff | per-`kind` events | history |

(`core/core.py` is intentionally excluded from the abstraction: its node
stream is the wire-protocol SSE boundary itself, not an inspector
attachment, and protocol stability matters more than dedup. We can revisit
once the abstraction has shaken out elsewhere.)

## What was extracted

`node_sdk/sse.py`:

- `SSEHub` — a fan-out hub holding `set[asyncio.Queue]` of subscribers.
  Producers call `hub.broadcast(name, data, event_id=None)`; the hub
  fans out via non-blocking `put_nowait` and drops events for slow
  consumers rather than blocking the producer (same behaviour as legacy).
- `add_subscriber(maxsize=1024)` / `remove_subscriber(q)`.
- `serve_sse(request, hub, *, replay=None, heartbeat_seconds=15.0,
  queue_maxsize=1024, extra_headers=None)` — owns the response loop:
  prepares the `text/event-stream` response, writes optional replay,
  drains the queue with heartbeat fallback, swallows
  `ConnectionResetError` / `BrokenPipeError` to survive client drops,
  and unregisters the queue in `finally`.
- `Last-Event-ID` resume: when items carry an event id (3-tuple form
  `(name, data, event_id)`), the server skips replay items up to and
  including the id sent by the client in `Last-Event-ID`. No-op when
  ids are absent.

## What was migrated

1. **`nodes/nexus_agent/web/server.py`**
   - `AgentInspectorState.subscribers: set[Queue]` → `hub: SSEHub`.
   - `publish()` now calls `self.hub.broadcast(kind, evt, event_id=evt["at"])`
     — the wire `data` is still the full event dict so the legacy
     frontend (`es.addEventListener(k, e => JSON.parse(e.data).data)`)
     keeps working.
   - The 30+ line `events()` handler is now three lines using `serve_sse`.
   - Replay is the last 100 history events with their ISO timestamps as
     ids (lets a reconnect skip events the client has already seen).

2. **`nodes/kanban_node/kanban_node.py`**
   - `KanbanBoard.subscribers: set[Queue]` → `hub: SSEHub`.
   - `_persist_and_push()` now calls
     `self.hub.broadcast("state", snap, event_id=snap["updated_at"])`.
   - Replay is a single fresh `("state", snapshot, updated_at)` tuple
     produced at connect time — not a stale buffer — matching legacy
     behaviour exactly.

## Wire-format diff

The wire format is bit-for-bit identical except that broadcasts now
include an `id:` line (the event timestamp). EventSource consumes `id:`
silently — it just stores it as `lastEventId` and replays it on reconnect
via `Last-Event-ID`. The `event:` and `data:` lines, the `: heartbeat`
comment, and the `\n\n` framing are unchanged. Heartbeat interval is
still 15 seconds. Behaviour on slow consumers (drop event, keep
connection) is unchanged.

## Left to migrate (follow-up work)

These five nodes still hand-roll their SSE loop. The migration is
mechanical now that the hub exists:

- `nodes/approval_node/approval_node.py` (108–133) — single `state`
  event, identical pattern to kanban; should be a 5-line change.
- `nodes/webui_node/webui_node.py` (80ff) — multi-kind publishes.
- `nodes/human_node/human_node.py` (125ff) — multi-kind publishes.
- `nodes/voice_actor/voice_actor.py` (680ff) — has the most state and
  some custom shaping; migrate carefully and verify the realtime
  transcript path.
- `nodes/nexus_agent_isolated/web/server.py` (90ff) — sibling of
  nexus_agent, history-replay pattern.

`core/core.py` is deliberately out of scope — it is the wire-protocol
boundary, not an inspector.

## Subtle behaviours preserved

- **Drop-on-full**: `broadcast()` uses `put_nowait` and silently drops
  for `QueueFull`. Slow consumers don't backpressure producers.
- **Heartbeat**: `: heartbeat\n\n` comment every 15s of idle.
- **Disconnect handling**: `ConnectionResetError` / `BrokenPipeError` on
  `response.write(...)` ends the loop without raising.
- **Replay timing**: kanban resnapshots at connect time (not from a
  buffer); nexus_agent replays its history list. Both match the
  pre-refactor implementations.

## Subtle behaviours that changed

- **`id:` line added to every event** (timestamp-derived). Spec-legal,
  silent for current frontends, enables `Last-Event-ID` resume on
  reconnect for clients that want it. If a frontend later starts
  parsing `e.lastEventId`, it gets sensible values for free.
