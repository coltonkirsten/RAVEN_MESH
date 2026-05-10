defmodule Mesh.Core do
  @moduledoc """
  Single GenServer holding manifest state — node declarations and the
  edge ACL — and routing invocations.

  Compared to the Python core (~430 lines), this lives entirely in
  Elixir messaging. There's no SSE plumbing, no asyncio queues, no
  pending-future bookkeeping: GenServer.call already gives us
  request/response with timeout for free, and the supervisor handles
  reconnection because nodes don't disconnect — they're processes.
  """
  use GenServer
  require Logger

  alias Mesh.{Crypto, NodeSupervisor, Tail}

  ## API

  def start_link(opts) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @doc "Load a manifest map (parsed) and start every node it declares."
  def load_manifest(parsed) do
    GenServer.call(__MODULE__, {:load_manifest, parsed})
  end

  @doc "Hot-add a node declaration without restarting the mesh."
  def add_node(decl, edges \\ []) do
    GenServer.call(__MODULE__, {:add_node, decl, edges})
  end

  @doc "Hot-remove a node by id (terminates its supervised process)."
  def remove_node(node_id) do
    GenServer.call(__MODULE__, {:remove_node, node_id})
  end

  @doc """
  Build, sign, and route an invocation envelope on behalf of a
  registered node. Used by tests and the demo script — it's the
  Elixir equivalent of POST /v0/admin/invoke + sign-from-secret.
  """
  def invoke(from_id, target, payload, opts \\ []) do
    GenServer.call(__MODULE__, {:invoke, from_id, target, payload, opts}, 35_000)
  end

  @doc "Route a fully-formed, externally-signed envelope (signature is checked)."
  def route(env) do
    GenServer.call(__MODULE__, {:route, env}, 35_000)
  end

  def introspect, do: GenServer.call(__MODULE__, :introspect)
  def secret_for(node_id), do: GenServer.call(__MODULE__, {:secret_for, node_id})

  ## Server

  @impl true
  def init(_opts) do
    {:ok,
     %{
       nodes: %{},
       edges: MapSet.new()
     }}
  end

  @impl true
  def handle_call({:load_manifest, parsed}, _from, _state) do
    nodes_map = parsed.nodes |> Enum.map(&{&1.id, &1}) |> Enum.into(%{})

    Enum.each(parsed.nodes, fn decl ->
      case NodeSupervisor.start_node(decl) do
        {:ok, _pid} -> :ok
        {:error, {:already_started, _}} -> :ok
        {:error, e} -> Logger.error("failed to start #{decl.id}: #{inspect(e)}")
      end
    end)

    {:reply, :ok, %{nodes: nodes_map, edges: parsed.edges}}
  end

  def handle_call({:add_node, decl, new_edges}, _from, state) do
    case NodeSupervisor.start_node(decl) do
      {:ok, _pid} ->
        edges = Enum.reduce(new_edges, state.edges, &MapSet.put(&2, &1))
        {:reply, :ok, %{state | nodes: Map.put(state.nodes, decl.id, decl), edges: edges}}

      {:error, e} ->
        {:reply, {:error, e}, state}
    end
  end

  def handle_call({:remove_node, id}, _from, state) do
    NodeSupervisor.stop_node(id)
    {:reply, :ok, %{state | nodes: Map.delete(state.nodes, id)}}
  end

  def handle_call({:invoke, from_id, target, payload, opts}, _from, state) do
    case Map.fetch(state.nodes, from_id) do
      {:ok, decl} ->
        env = build_envelope(from_id, target, payload, decl.secret, opts)
        {reply, env_with_status} = do_route(env, state, true)
        Tail.broadcast(env_with_status)
        {:reply, reply, state}

      :error ->
        {:reply, {:error, :unknown_node, %{}}, state}
    end
  end

  def handle_call({:route, env}, _from, state) do
    {reply, env_with_status} = do_route(env, state, false)
    Tail.broadcast(env_with_status)
    {:reply, reply, state}
  end

  def handle_call(:introspect, _from, state) do
    nodes =
      state.nodes
      |> Enum.map(fn {id, d} ->
        %{
          id: id,
          kind: d.kind,
          module: d.module,
          connected: connected?(id),
          surfaces: Map.values(d.surfaces)
        }
      end)

    edges = state.edges |> MapSet.to_list() |> Enum.map(fn {f, t} -> %{from: f, to: t} end)
    {:reply, %{nodes: nodes, edges: edges}, state}
  end

  def handle_call({:secret_for, id}, _from, state) do
    {:reply, get_in(state.nodes, [id, Access.key(:secret)]), state}
  end

  ## Internals

  defp build_envelope(from_id, target, payload, secret, opts) do
    msg_id = uuid()

    env = %{
      "id" => msg_id,
      "correlation_id" => Keyword.get(opts, :correlation_id, msg_id),
      "from" => from_id,
      "to" => target,
      "kind" => "invocation",
      "payload" => payload,
      "timestamp" => DateTime.utc_now() |> DateTime.to_iso8601()
    }

    Crypto.attach_signature(env, secret)
  end

  defp do_route(env, state, signature_pre_verified) do
    from_id = env["from"]
    to = env["to"]

    with {:ok, _from_decl} <- Map.fetch(state.nodes, from_id) |> ok_or(:unknown_node),
         :ok <- check_signature(env, state, signature_pre_verified),
         {:ok, target_id, surface_name} <- parse_to(to),
         :ok <- check_edge(state.edges, from_id, to),
         {:ok, target_decl} <- Map.fetch(state.nodes, target_id) |> ok_or(:unknown_node),
         {:ok, surface} <- Map.fetch(target_decl.surfaces, surface_name) |> ok_or(:unknown_surface) do
      deliver(env, target_id, surface)
      |> tap_status(env)
    else
      {:error, reason} ->
        {{:error, reason, %{}}, Map.put(env, "_route_status", to_string(reason))}
    end
  end

  defp ok_or({:ok, v}, _err), do: {:ok, v}
  defp ok_or(:error, err), do: {:error, err}

  defp parse_to(nil), do: {:error, :bad_target}

  defp parse_to(to) when is_binary(to) do
    case String.split(to, ".", parts: 2) do
      [target, surface] -> {:ok, target, surface}
      _ -> {:error, :bad_target}
    end
  end

  defp check_signature(_env, _state, true), do: :ok

  defp check_signature(env, state, false) do
    case Map.fetch(state.nodes, env["from"]) do
      {:ok, decl} ->
        if Crypto.verify(env, decl.secret), do: :ok, else: {:error, :bad_signature}

      :error ->
        {:error, :unknown_node}
    end
  end

  defp check_edge(edges, from, to) do
    if MapSet.member?(edges, {from, to}), do: :ok, else: {:error, :denied_no_relationship}
  end

  defp deliver(env, target_id, surface) do
    case Registry.lookup(Mesh.NodeRegistry, target_id) do
      [{pid, _}] ->
        case surface.invocation_mode do
          :fire_and_forget ->
            GenServer.cast(pid, {:invoke, env})
            {:ok, %{"status" => "accepted", "id" => env["id"]}}

          :request_response ->
            try do
              case GenServer.call(pid, {:invoke, env}, 30_000) do
                {:ok, payload} -> {:ok, payload}
                {:error, reason, details} -> {:error, reason, details}
              end
            catch
              :exit, {:timeout, _} -> {:error, :timeout, %{}}
              :exit, {:noproc, _} -> {:error, :node_unreachable, %{}}
            end
        end

      [] ->
        {:error, :node_unreachable, %{}}
    end
  end

  defp tap_status(result, env) do
    status =
      case result do
        {:ok, _} -> "routed"
        {:error, r, _} -> to_string(r)
        {:error, r} -> to_string(r)
      end

    {result, Map.put(env, "_route_status", status)}
  end

  defp connected?(id) do
    case Registry.lookup(Mesh.NodeRegistry, id) do
      [_] -> true
      [] -> false
    end
  end

  defp uuid do
    :crypto.strong_rand_bytes(16)
    |> Base.encode16(case: :lower)
  end
end
