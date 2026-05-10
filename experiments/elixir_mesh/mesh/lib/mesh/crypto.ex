defmodule Mesh.Crypto do
  @moduledoc """
  Wire-compatible HMAC-SHA256 signing that matches the Python core.

  Python canonical form:
    json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
  where `body` is the envelope minus the `signature` key.

  We hand-roll the canonical JSON to guarantee key ordering — Erlang
  maps don't preserve order, and Jason.encode! gives no order
  guarantee for large maps. This way the wire format is identical to
  Python's, byte for byte.
  """

  @spec canonical(map()) :: binary()
  def canonical(envelope) when is_map(envelope) do
    envelope
    |> Map.delete("signature")
    |> Map.delete(:signature)
    |> encode_value()
    |> IO.iodata_to_binary()
  end

  defp encode_value(map) when is_map(map) do
    pairs =
      map
      |> Enum.map(fn {k, v} -> {to_string(k), v} end)
      |> Enum.sort_by(fn {k, _} -> k end)
      |> Enum.map(fn {k, v} ->
        [Jason.encode!(k), ":", encode_value(v)]
      end)
      |> Enum.intersperse(",")

    ["{", pairs, "}"]
  end

  defp encode_value(list) when is_list(list) do
    items =
      list
      |> Enum.map(&encode_value/1)
      |> Enum.intersperse(",")

    ["[", items, "]"]
  end

  defp encode_value(nil), do: "null"
  defp encode_value(true), do: "true"
  defp encode_value(false), do: "false"
  defp encode_value(n) when is_integer(n), do: Integer.to_string(n)
  defp encode_value(f) when is_float(f), do: Jason.encode!(f)
  defp encode_value(b) when is_binary(b), do: Jason.encode!(b)
  defp encode_value(a) when is_atom(a), do: Jason.encode!(Atom.to_string(a))

  @spec sign(map(), binary()) :: binary()
  def sign(envelope, secret) when is_binary(secret) do
    :crypto.mac(:hmac, :sha256, secret, canonical(envelope))
    |> Base.encode16(case: :lower)
  end

  @spec verify(map(), binary()) :: boolean()
  def verify(envelope, secret) do
    case Map.get(envelope, "signature") || Map.get(envelope, :signature) do
      sig when is_binary(sig) ->
        expected = sign(envelope, secret)
        constant_time_eq?(sig, expected)

      _ ->
        false
    end
  end

  defp constant_time_eq?(a, b) when byte_size(a) == byte_size(b) do
    :crypto.hash_equals(a, b)
  end

  defp constant_time_eq?(_, _), do: false

  @spec attach_signature(map(), binary()) :: map()
  def attach_signature(envelope, secret) do
    Map.put(envelope, "signature", sign(envelope, secret))
  end
end
