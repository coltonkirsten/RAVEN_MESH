defmodule Mesh.Node do
  @moduledoc """
  Behaviour + helper for node implementations.

  A node is just a GenServer registered in `Mesh.NodeRegistry` under
  its `node_id`. The Core sends two kinds of messages to it:

    {:invoke, env}                 fire-and-forget   (handle_cast)
    {:invoke, env, {pid, ref}}     request/response  (the node sends
                                                      {ref, response} back)

  Implementing module overrides `handle_surface/3`, which receives
  the surface name, payload, and node state, and returns one of:

    {:reply, payload_map, new_state}     -> envelope kind="response"
    {:error, reason, details, new_state} -> envelope kind="error"
    {:noreply, new_state}                -> only valid for fire_and_forget

  The base GenServer machinery (lifecycle, registration, dispatch) is
  injected via `use Mesh.Node`.
  """

  @callback init_state(decl :: map()) :: any()
  @callback handle_surface(surface :: String.t(), payload :: map(), state :: any()) ::
              {:reply, map(), any()}
              | {:error, String.t(), map(), any()}
              | {:noreply, any()}

  defmacro __using__(_opts) do
    quote do
      use GenServer
      @behaviour Mesh.Node
      require Logger

      def start_link(decl) do
        GenServer.start_link(__MODULE__, decl,
          name: {:via, Registry, {Mesh.NodeRegistry, decl.id}}
        )
      end

      @impl GenServer
      def init(decl) do
        state = %{
          decl: decl,
          inner: init_state(decl)
        }

        {:ok, state}
      end

      @impl GenServer
      def handle_cast({:invoke, env}, state) do
        new_inner =
          case dispatch(env, state) do
            {:noreply, ni} -> ni
            {:reply, _payload, ni} -> ni
            {:error, _r, _d, ni} -> ni
          end

        {:noreply, %{state | inner: new_inner}}
      end

      @impl GenServer
      def handle_call({:invoke, env}, _from, state) do
        {reply, new_inner} =
          case dispatch(env, state) do
            {:reply, payload, ni} -> {{:ok, payload}, ni}
            {:error, reason, details, ni} -> {{:error, reason, details}, ni}
            {:noreply, ni} -> {{:ok, %{}}, ni}
          end

        {:reply, reply, %{state | inner: new_inner}}
      end

      def handle_call(:ping, _from, state), do: {:reply, :pong, state}
      def handle_call(:state, _from, state), do: {:reply, state.inner, state}

      defp dispatch(env, state) do
        surface =
          case env["to"] do
            nil -> nil
            to -> List.last(String.split(to, ".", parts: 2))
          end

        try do
          handle_surface(surface, Map.get(env, "payload", %{}), state.inner)
        rescue
          e ->
            Logger.error("[#{state.decl.id}] handler crash: #{Exception.message(e)}")
            {:error, "handler_exception", %{"details" => Exception.message(e)}, state.inner}
        end
      end

      def init_state(_decl), do: %{}

      defoverridable init_state: 1
    end
  end
end
