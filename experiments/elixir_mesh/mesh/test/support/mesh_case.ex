defmodule Mesh.Case do
  @moduledoc """
  Shared test setup. Each test resets Core to a fresh manifest so
  tests don't leak state between each other.
  """
  use ExUnit.CaseTemplate

  using do
    quote do
      import Mesh.Case
    end
  end

  setup do
    # Stop every running node, then reload a clean manifest per-test.
    for {_, pid, _, _} <- DynamicSupervisor.which_children(Mesh.NodeSupervisor) do
      DynamicSupervisor.terminate_child(Mesh.NodeSupervisor, pid)
    end

    :ok
  end

  def fresh_manifest do
    %{
      nodes: [
        node_decl("voice_actor", Mesh.EchoNode, [
          {"inbox", :fire_and_forget},
          {"echo", :request_response},
          {"crash", :request_response}
        ]),
        node_decl("kanban", Mesh.KanbanNode, [
          {"add_card", :request_response},
          {"list_cards", :request_response},
          {"move_card", :request_response}
        ])
      ],
      edges:
        MapSet.new([
          {"voice_actor", "kanban.add_card"},
          {"voice_actor", "kanban.list_cards"},
          {"voice_actor", "kanban.move_card"},
          {"voice_actor", "voice_actor.echo"},
          {"voice_actor", "voice_actor.crash"}
        ])
    }
  end

  def node_decl(id, module, surfaces) do
    %{
      id: id,
      kind: "test",
      module: module,
      secret: id <> "-secret",
      surfaces:
        surfaces
        |> Enum.map(fn {name, mode} ->
          {name, %{name: name, type: "tool", invocation_mode: mode}}
        end)
        |> Enum.into(%{})
    }
  end

  def boot(manifest \\ nil) do
    manifest = manifest || fresh_manifest()
    :ok = Mesh.Core.load_manifest(manifest)
    Process.sleep(20)
    :ok
  end
end
