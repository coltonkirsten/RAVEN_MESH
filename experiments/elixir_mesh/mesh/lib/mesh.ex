defmodule Mesh do
  @moduledoc """
  Top-level convenience API. Most callers should reach for
  `Mesh.Core` directly; this module just exposes a few common
  one-liners used by the demo script and tests.
  """

  defdelegate invoke(from_id, target, payload), to: Mesh.Core
  defdelegate invoke(from_id, target, payload, opts), to: Mesh.Core
  defdelegate add_node(decl, edges), to: Mesh.Core
  defdelegate remove_node(id), to: Mesh.Core
  defdelegate introspect(), to: Mesh.Core

  @doc """
  Boot a manifest from a JSON file path. Used by `bin/demo.exs` and
  `mix run`.
  """
  def boot(manifest_path) do
    {:ok, parsed} = Mesh.Manifest.load(manifest_path)
    Mesh.Core.load_manifest(parsed)
  end
end
