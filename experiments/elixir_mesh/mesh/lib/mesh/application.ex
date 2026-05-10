defmodule Mesh.Application do
  @moduledoc false
  use Application

  @impl true
  def start(_type, _args) do
    children = [
      {Phoenix.PubSub, name: Mesh.PubSub},
      {Registry, keys: :unique, name: Mesh.NodeRegistry},
      Mesh.NodeSupervisor,
      Mesh.Core
    ]

    opts = [strategy: :one_for_one, name: Mesh.Supervisor]
    Supervisor.start_link(children, opts)
  end
end
