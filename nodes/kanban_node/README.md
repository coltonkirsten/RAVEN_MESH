# kanban_node

Capability node that exposes a kanban board as mesh tool surfaces, with a
live web UI on port 8805 (default).

Other nodes can create, move, update, delete cards, list/filter, and add or
remove columns. Browser users can drag cards between columns; agent
mutations broadcast via SSE so all connected browsers update in real time.

## Run

```bash
scripts/run_core.sh --manifest manifests/kanban_demo.yaml &
scripts/run_kanban.sh &
open http://127.0.0.1:8805
```

Default columns on first boot: **Backlog**, **In Progress**, **Review**, **Done**.

State is persisted to `nodes/kanban_node/data/board.json` and reloaded on
restart.

## Tool surfaces

| surface                       | payload                                          | returns                  |
| ----------------------------- | ------------------------------------------------ | ------------------------ |
| `kanban_node.create_card`     | `{column, title, description?, tags?}`           | `{card_id, card}`        |
| `kanban_node.move_card`       | `{card_id, to_column}`                           | `{ok, card}`             |
| `kanban_node.update_card`     | `{card_id, title?, description?, tags?}`         | `{ok, card}`             |
| `kanban_node.delete_card`     | `{card_id}`                                      | `{deleted, card_id}`     |
| `kanban_node.list_cards`      | `{column?}`                                      | `{cards}`                |
| `kanban_node.get_board`       | `{}`                                             | `{columns, cards, ...}`  |
| `kanban_node.add_column`      | `{name, position?}`                              | `{ok, column}`           |
| `kanban_node.delete_column`   | `{name}` — rejected if column has cards          | `{ok, name}` or error    |
| `kanban_node.ui_visibility`   | `{action: 'show' \| 'hide'}`                     | `{ok, hidden}`           |
| `kanban_node.status_get`      | `{}`                                             | `{hidden, cards, ...}`   |

When `ui_visibility('hide')` is invoked the web UI returns 503 to all browser
HTTP routes (the `/events` SSE channel stays open so connected browsers see
the un-hide). Mesh tool invocations continue working normally — the node
remains fully functional, just with no UI surface.

## Local browser-only HTTP routes

Browser-driven mutations go through the same kanban_node web server and call
the same internal mutator functions, so SSE updates fan out regardless of
source.

- `POST   /api/cards`              — create
- `PATCH  /api/cards/{card_id}`    — update / move (`to_column` to move)
- `DELETE /api/cards/{card_id}`    — delete
- `POST   /api/columns`            — add
- `DELETE /api/columns/{name}`     — delete (must be empty)
- `GET    /state`                  — board snapshot
- `GET    /events`                 — SSE board updates
