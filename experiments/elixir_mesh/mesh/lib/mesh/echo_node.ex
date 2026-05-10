defmodule Mesh.EchoNode do
  @moduledoc """
  Trivial node: returns the payload it was sent. Surface contract:
    inbox / fire_and_forget  -> swallows
    echo  / request_response -> echoes payload back
  """
  use Mesh.Node

  @impl Mesh.Node
  def init_state(_decl), do: %{seen: 0}

  @impl Mesh.Node
  def handle_surface("echo", payload, state) do
    {:reply, %{"echo" => payload, "seen" => state.seen + 1},
     %{state | seen: state.seen + 1}}
  end

  def handle_surface("inbox", _payload, state) do
    {:noreply, %{state | seen: state.seen + 1}}
  end

  def handle_surface("crash", _payload, _state) do
    raise "intentional crash"
  end

  def handle_surface(other, _payload, state) do
    {:error, "unknown_surface", %{"surface" => other}, state}
  end
end
