#!/usr/bin/env elixir
# Demo script — run with: mix run bin/demo.exs

defmodule Demo do
  def banner(text) do
    IO.puts("")
    IO.puts(IO.ANSI.cyan() <> "── #{text} ──" <> IO.ANSI.reset())
  end
end

Demo.banner("1. Boot manifest")
Mesh.Tail.subscribe()
{:ok, parsed} = Mesh.Manifest.load("manifests/demo.json")
:ok = Mesh.Core.load_manifest(parsed)
Process.sleep(50)
IO.inspect(Mesh.Core.introspect(), label: "introspect")

Demo.banner("2. Invoke kanban.add_card via voice_actor")
{:ok, r1} = Mesh.invoke("voice_actor", "kanban.add_card",
                        %{"column" => "todo", "title" => "build elixir prototype"})
IO.inspect(r1, label: "add_card")

{:ok, r2} = Mesh.invoke("voice_actor", "kanban.add_card",
                        %{"column" => "doing", "title" => "ship demo"})
IO.inspect(r2, label: "add_card")

{:ok, listing} = Mesh.invoke("voice_actor", "kanban.list_cards", %{})
IO.inspect(listing, label: "list_cards")

Demo.banner("3. Edge ACL — voice_actor cannot reach an unrelated node")
result = Mesh.invoke("voice_actor", "kanban.does_not_exist", %{})
IO.inspect(result, label: "denied (unknown surface) ->")

Demo.banner("4. Hot-add a third node WITHOUT restarting the mesh")
new_decl = %{
  id: "echo2",
  kind: "capability",
  module: Mesh.EchoNode,
  secret: "echo2-secret",
  surfaces: %{
    "echo" => %{name: "echo", type: "tool", invocation_mode: :request_response}
  }
}

:ok = Mesh.add_node(new_decl, [{"voice_actor", "echo2.echo"}])
{:ok, r3} = Mesh.invoke("voice_actor", "echo2.echo", %{"hello" => "world"})
IO.inspect(r3, label: "echo2 response")

Demo.banner("5. Crash recovery — kill kanban, supervisor restarts it")
[{kanban_pid, _}] = Registry.lookup(Mesh.NodeRegistry, "kanban")
IO.puts("kanban pid before crash: #{inspect(kanban_pid)}")
ref = Process.monitor(kanban_pid)
Process.exit(kanban_pid, :kill)

receive do
  {:DOWN, ^ref, _, _, reason} -> IO.puts("kanban exited: #{inspect(reason)}")
after
  1_000 -> IO.puts("(timed out waiting for DOWN)")
end

# Supervisor restarts kanban under the same registered name within ms.
Process.sleep(50)
[{after_pid, _}] = Registry.lookup(Mesh.NodeRegistry, "kanban")
IO.puts("kanban pid after restart: #{inspect(after_pid)}")
IO.puts("Same process? #{after_pid == kanban_pid}")

# State was lost on crash — that's the contract. Add a card again to
# prove routing works post-restart.
{:ok, r4} = Mesh.invoke("voice_actor", "kanban.add_card",
                        %{"column" => "todo", "title" => "post-restart card"})
IO.inspect(r4, label: "post-restart add_card")

Demo.banner("6. Tail subscriber — drain envelopes broadcast to PubSub")
envelopes =
  Stream.repeatedly(fn ->
    receive do
      {:envelope, env} -> env
    after
      0 -> :done
    end
  end)
  |> Enum.take_while(&(&1 != :done))

IO.puts("captured #{length(envelopes)} envelopes via PubSub tail")

Enum.each(Enum.take(envelopes, 3), fn env ->
  IO.puts("  #{env["from"]} → #{env["to"]} [#{env["_route_status"]}]")
end)

IO.puts("")
IO.puts(IO.ANSI.green() <> "✓ demo complete" <> IO.ANSI.reset())
