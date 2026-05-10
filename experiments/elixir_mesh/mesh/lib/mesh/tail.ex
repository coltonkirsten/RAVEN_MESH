defmodule Mesh.Tail do
  @moduledoc """
  Phoenix.PubSub-backed envelope tail. Replaces the Python core's
  SSE admin stream with a few lines of code.

  Subscribers `Mesh.Tail.subscribe/0` and receive
    {:envelope, env}
  for every envelope routed through Core.
  """

  @pubsub Mesh.PubSub
  @topic "envelope_tail"

  def subscribe do
    Phoenix.PubSub.subscribe(@pubsub, @topic)
  end

  def broadcast(env) do
    Phoenix.PubSub.broadcast(@pubsub, @topic, {:envelope, env})
  end
end
