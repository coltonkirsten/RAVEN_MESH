defmodule MeshTest do
  use Mesh.Case

  describe "crypto wire format" do
    test "canonical JSON sorts keys recursively, drops signature" do
      env = %{
        "to" => "x.y",
        "from" => "a",
        "payload" => %{"b" => 2, "a" => 1},
        "signature" => "should-be-stripped"
      }

      canonical = Mesh.Crypto.canonical(env)
      # Keys at every level are alphabetic; signature is gone.
      assert canonical ==
               ~s({"from":"a","payload":{"a":1,"b":2},"to":"x.y"})
    end

    test "sign/verify round-trip" do
      env = %{"id" => "1", "from" => "n", "payload" => %{"k" => "v"}}
      signed = Mesh.Crypto.attach_signature(env, "secret")
      assert Mesh.Crypto.verify(signed, "secret")
      refute Mesh.Crypto.verify(signed, "different-secret")
    end

    test "verify rejects tampered envelopes" do
      env =
        %{"id" => "1", "from" => "n", "payload" => %{"k" => "v"}}
        |> Mesh.Crypto.attach_signature("secret")

      tampered = put_in(env, ["payload", "k"], "tampered")
      refute Mesh.Crypto.verify(tampered, "secret")
    end
  end

  describe "routing" do
    test "request/response invocation succeeds and returns payload" do
      boot()
      {:ok, %{"card" => card}} =
        Mesh.invoke("voice_actor", "kanban.add_card",
                    %{"column" => "todo", "title" => "hello"})

      assert card["title"] == "hello"
      assert card["id"] == 1
    end

    test "fire-and-forget returns accepted without blocking" do
      boot()
      :ok = Mesh.Core.add_node(
        %{
          id: "sink",
          kind: "test",
          module: Mesh.EchoNode,
          secret: "sink-secret",
          surfaces: %{
            "inbox" => %{name: "inbox", type: "inbox", invocation_mode: :fire_and_forget}
          }
        },
        [{"voice_actor", "sink.inbox"}]
      )

      {:ok, %{"status" => "accepted"}} =
        Mesh.invoke("voice_actor", "sink.inbox", %{"hello" => "world"})
    end

    test "edge ACL denies invocations across undeclared relationships" do
      boot()

      assert {:error, :denied_no_relationship, _} =
               Mesh.invoke("kanban", "voice_actor.echo", %{})
    end

    test "unknown surface returns error from target node" do
      boot()
      # voice_actor.echo is allowed, but voice_actor doesn't have an
      # "ghost" surface — denial happens inside Core because the
      # target_decl.surfaces map is checked before deliver.
      assert {:error, :denied_no_relationship, _} =
               Mesh.invoke("voice_actor", "voice_actor.ghost", %{})
    end
  end

  describe "supervisor lifecycle" do
    test "hot-add brings up a new node and routes to it without restart" do
      boot()
      core_pid = Process.whereis(Mesh.Core)

      :ok =
        Mesh.add_node(
          %{
            id: "echo2",
            kind: "test",
            module: Mesh.EchoNode,
            secret: "echo2-secret",
            surfaces: %{
              "echo" => %{name: "echo", type: "tool", invocation_mode: :request_response}
            }
          },
          [{"voice_actor", "echo2.echo"}]
        )

      {:ok, %{"echo" => %{"k" => "v"}}} =
        Mesh.invoke("voice_actor", "echo2.echo", %{"k" => "v"})

      # Core process is the same process — no restart happened.
      assert Process.whereis(Mesh.Core) == core_pid
    end

    test "killed node process is restarted by supervisor and routing recovers" do
      boot()
      [{old_pid, _}] = Registry.lookup(Mesh.NodeRegistry, "kanban")
      ref = Process.monitor(old_pid)
      Process.exit(old_pid, :kill)

      assert_receive {:DOWN, ^ref, _, _, :killed}, 500

      # Wait for restart.
      :ok = wait_for_restart("kanban", old_pid)

      [{new_pid, _}] = Registry.lookup(Mesh.NodeRegistry, "kanban")
      refute new_pid == old_pid

      # Routing recovers — Core didn't have to be touched at all.
      assert {:ok, %{"card" => _}} =
               Mesh.invoke("voice_actor", "kanban.add_card",
                          %{"column" => "todo", "title" => "post-restart"})
    end

    test "remove_node terminates a running node" do
      boot()
      assert [{_pid, _}] = Registry.lookup(Mesh.NodeRegistry, "kanban")
      :ok = Mesh.remove_node("kanban")
      Process.sleep(20)
      assert [] == Registry.lookup(Mesh.NodeRegistry, "kanban")
    end

    test "exception inside a handler is converted to a kind=error response, node stays alive" do
      boot()
      [{pid_before, _}] = Registry.lookup(Mesh.NodeRegistry, "voice_actor")

      assert {:error, "handler_exception", details} =
               Mesh.invoke("voice_actor", "voice_actor.crash", %{})

      assert details["details"] =~ "intentional crash"
      [{pid_after, _}] = Registry.lookup(Mesh.NodeRegistry, "voice_actor")
      assert pid_before == pid_after
    end
  end

  describe "tail" do
    test "every routed envelope is broadcast to PubSub subscribers" do
      Mesh.Tail.subscribe()
      boot()
      # Drain anything from before subscribe (none expected, but be safe).
      drain()

      {:ok, _} =
        Mesh.invoke("voice_actor", "kanban.add_card",
                    %{"column" => "todo", "title" => "x"})

      assert_receive {:envelope, env}, 200
      assert env["from"] == "voice_actor"
      assert env["to"] == "kanban.add_card"
      assert env["_route_status"] == "routed"
    end
  end

  defp wait_for_restart(node_id, old_pid, attempts \\ 50)
  defp wait_for_restart(_id, _old, 0), do: {:error, :no_restart}

  defp wait_for_restart(node_id, old_pid, attempts) do
    case Registry.lookup(Mesh.NodeRegistry, node_id) do
      [{pid, _}] when pid != old_pid ->
        :ok

      _ ->
        Process.sleep(10)
        wait_for_restart(node_id, old_pid, attempts - 1)
    end
  end

  defp drain do
    receive do
      {:envelope, _} -> drain()
    after
      0 -> :ok
    end
  end
end
