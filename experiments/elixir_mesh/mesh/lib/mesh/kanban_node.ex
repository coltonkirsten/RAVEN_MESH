defmodule Mesh.KanbanNode do
  @moduledoc """
  Tiny in-memory kanban board. Demonstrates per-node state living
  inside its own GenServer — when the supervisor restarts the
  process, state resets, which is the honest BEAM default. (For
  durable state you'd hand the board off to a separate `Agent` or
  persist via an init callback; we don't here, on purpose, so the
  crash-recovery demo is visible.)

  Surfaces:
    add_card    request_response  payload: %{"column", "title"}
    list_cards  request_response  payload: %{}
    move_card   request_response  payload: %{"id", "column"}
  """
  use Mesh.Node

  @impl Mesh.Node
  def init_state(_decl) do
    %{
      next_id: 1,
      cards: %{},
      columns: ["todo", "doing", "done"]
    }
  end

  @impl Mesh.Node
  def handle_surface("add_card", %{"column" => col, "title" => title}, state) do
    if col in state.columns do
      id = state.next_id
      card = %{"id" => id, "title" => title, "column" => col}
      new_state = %{state | next_id: id + 1, cards: Map.put(state.cards, id, card)}
      {:reply, %{"card" => card}, new_state}
    else
      {:error, "unknown_column", %{"column" => col, "valid" => state.columns}, state}
    end
  end

  def handle_surface("list_cards", _payload, state) do
    {:reply, %{"cards" => Map.values(state.cards)}, state}
  end

  def handle_surface("move_card", %{"id" => id, "column" => col}, state) do
    cond do
      not Map.has_key?(state.cards, id) ->
        {:error, "unknown_card", %{"id" => id}, state}

      col not in state.columns ->
        {:error, "unknown_column", %{"column" => col}, state}

      true ->
        card = Map.fetch!(state.cards, id) |> Map.put("column", col)
        {:reply, %{"card" => card}, %{state | cards: Map.put(state.cards, id, card)}}
    end
  end

  def handle_surface(other, _payload, state) do
    {:error, "unknown_surface", %{"surface" => other}, state}
  end
end
