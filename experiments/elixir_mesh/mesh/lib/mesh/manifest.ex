defmodule Mesh.Manifest do
  @moduledoc """
  Loads a manifest describing nodes (id, kind, surfaces, secret) and
  relationships (from -> to.surface).

  We accept JSON manifests instead of the Python prototype's YAML to
  avoid pulling in a YAML dep — the structure is identical.

  Example:
    {
      "nodes": [
        {
          "id": "voice_actor",
          "kind": "actor",
          "module": "Mesh.EchoNode",
          "secret": "voice-secret",
          "surfaces": [
            {"name": "inbox", "type": "inbox", "invocation_mode": "fire_and_forget"}
          ]
        }
      ],
      "relationships": [{"from": "voice_actor", "to": "tasks.create"}]
    }
  """

  @type surface :: %{
          name: String.t(),
          type: String.t(),
          invocation_mode: :request_response | :fire_and_forget
        }

  @type node_decl :: %{
          id: String.t(),
          kind: String.t(),
          module: module(),
          secret: String.t(),
          surfaces: %{String.t() => surface()}
        }

  @spec load(String.t()) :: {:ok, %{nodes: [node_decl()], edges: MapSet.t()}} | {:error, any()}
  def load(path) do
    with {:ok, raw} <- File.read(path),
         {:ok, data} <- Jason.decode(raw) do
      nodes = data |> Map.get("nodes", []) |> Enum.map(&parse_node/1)
      edges =
        data
        |> Map.get("relationships", [])
        |> Enum.map(fn r -> {r["from"], r["to"]} end)
        |> MapSet.new()

      {:ok, %{nodes: nodes, edges: edges}}
    end
  end

  @spec parse(map()) :: %{nodes: [node_decl()], edges: MapSet.t()}
  def parse(data) when is_map(data) do
    %{
      nodes: data |> Map.get("nodes", []) |> Enum.map(&parse_node/1),
      edges:
        data
        |> Map.get("relationships", [])
        |> Enum.map(fn r -> {r["from"], r["to"]} end)
        |> MapSet.new()
    }
  end

  defp parse_node(n) do
    %{
      id: n["id"],
      kind: n["kind"],
      module: resolve_module(n["module"]),
      secret: n["secret"] || autogen_secret(n["id"]),
      surfaces:
        n
        |> Map.get("surfaces", [])
        |> Enum.map(fn s ->
          {s["name"],
           %{
             name: s["name"],
             type: s["type"],
             invocation_mode: parse_mode(s["invocation_mode"])
           }}
        end)
        |> Enum.into(%{})
    }
  end

  defp resolve_module(nil), do: Mesh.EchoNode

  defp resolve_module(name) when is_binary(name) do
    String.to_existing_atom("Elixir." <> name)
  rescue
    ArgumentError ->
      raise "unknown node module: #{name} (must be loaded already)"
  end

  defp parse_mode("fire_and_forget"), do: :fire_and_forget
  defp parse_mode(_), do: :request_response

  defp autogen_secret(node_id) do
    :crypto.hash(:sha256, "mesh:#{node_id}:autogen") |> Base.encode16(case: :lower)
  end
end
