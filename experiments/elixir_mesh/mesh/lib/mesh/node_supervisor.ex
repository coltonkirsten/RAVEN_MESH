defmodule Mesh.NodeSupervisor do
  @moduledoc """
  DynamicSupervisor that owns every running node process.

  Hot-add: start_node/1 spawns a fresh GenServer at runtime.
  Hot-stop: stop_node/1 terminates by node_id.
  Crash recovery: nodes are :transient — supervisor restarts on
  abnormal exit, leaves them down on normal stop.
  """
  use DynamicSupervisor

  def start_link(_opts) do
    DynamicSupervisor.start_link(__MODULE__, :ok, name: __MODULE__)
  end

  @impl true
  def init(:ok) do
    DynamicSupervisor.init(strategy: :one_for_one, max_restarts: 10, max_seconds: 5)
  end

  @doc """
  Spawn a node from a declaration. The declaration is the same shape
  as Mesh.Manifest.node_decl/0. The node's GenServer registers itself
  in Mesh.NodeRegistry under its id.
  """
  def start_node(decl) do
    spec = %{
      id: {:node, decl.id},
      start: {decl.module, :start_link, [decl]},
      restart: :transient,
      type: :worker,
      shutdown: 5_000
    }

    DynamicSupervisor.start_child(__MODULE__, spec)
  end

  def stop_node(node_id) do
    case Registry.lookup(Mesh.NodeRegistry, node_id) do
      [{pid, _}] -> DynamicSupervisor.terminate_child(__MODULE__, pid)
      [] -> {:error, :not_found}
    end
  end

  def list_running do
    DynamicSupervisor.which_children(__MODULE__)
    |> Enum.map(fn {_, pid, _, _} -> pid end)
    |> Enum.filter(&is_pid/1)
  end
end
