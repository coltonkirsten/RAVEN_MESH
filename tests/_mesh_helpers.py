"""Helpers for building ephemeral mesh manifests inside tests.

Tests use these helpers to construct a self-contained manifest (nodes,
edges, surface schemas) in ``tmp_path`` and boot Core against it. No
test depends on any shipped manifest or schema file — each test
declares the mesh shape it wants and tears it down.

Returned manifests pass ``core.manifest_validator.validate_manifest``
without modification, so they can be fed directly to ``make_app``.
"""
from __future__ import annotations

import json
import pathlib
import socket
from typing import Iterable

import yaml


PERMISSIVE_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": True,
}


def minimal_surface(
    name: str,
    *,
    schema_path: str = "../schemas/permissive.json",
    type_: str = "tool",
    invocation_mode: str = "request_response",
) -> dict:
    """Return a surface dict referencing ``schema_path`` (relative to manifest dir)."""
    return {
        "name": name,
        "type": type_,
        "invocation_mode": invocation_mode,
        "schema": schema_path,
    }


def minimal_actor(
    node_id: str,
    *,
    surfaces: list[dict] | None = None,
    secret_env: str | None = None,
) -> dict:
    """Build a minimal actor node. ``secret_env`` controls the identity_secret."""
    out: dict = {
        "id": node_id,
        "kind": "actor",
        "runtime": "local-process",
        "surfaces": list(surfaces or []),
    }
    if secret_env is not None:
        out["identity_secret"] = f"env:{secret_env}"
    return out


def minimal_capability(
    node_id: str,
    *,
    surfaces: list[dict],
    secret_env: str | None = None,
) -> dict:
    """Build a minimal capability node."""
    out: dict = {
        "id": node_id,
        "kind": "capability",
        "runtime": "local-process",
        "surfaces": list(surfaces),
    }
    if secret_env is not None:
        out["identity_secret"] = f"env:{secret_env}"
    return out


def minimal_approval(
    node_id: str,
    *,
    surfaces: list[dict],
    secret_env: str | None = None,
) -> dict:
    """Build a minimal approval node (kind=approval)."""
    out: dict = {
        "id": node_id,
        "kind": "approval",
        "runtime": "local-process",
        "surfaces": list(surfaces),
    }
    if secret_env is not None:
        out["identity_secret"] = f"env:{secret_env}"
    return out


def build_ephemeral_manifest(
    tmp_path: pathlib.Path,
    nodes: list[dict],
    edges: Iterable[tuple[str, str]] | None = None,
    *,
    schemas: dict[str, dict] | None = None,
) -> pathlib.Path:
    """Write a complete ephemeral mesh under ``tmp_path`` and return the manifest path.

    Layout: ``tmp_path/manifests/test.yaml`` + ``tmp_path/schemas/*.json``.
    Every surface schema referenced by ``nodes`` is materialized — if a
    schema path isn't in ``schemas``, a permissive object schema is
    written for it. Schemas are addressed via ``../schemas/<name>``
    relative to the manifest, matching the live convention.
    """
    manifests_dir = tmp_path / "manifests"
    schemas_dir = tmp_path / "schemas"
    manifests_dir.mkdir(exist_ok=True)
    schemas_dir.mkdir(exist_ok=True)

    needed: set[str] = set()
    for n in nodes:
        for s in n.get("surfaces", []):
            sp = s.get("schema", "")
            if sp.startswith("../schemas/"):
                needed.add(sp[len("../schemas/"):])

    schemas = schemas or {}
    for name in needed:
        body = schemas.get(name, PERMISSIVE_SCHEMA)
        (schemas_dir / name).write_text(json.dumps(body))

    manifest = {
        "nodes": nodes,
        "relationships": [{"from": a, "to": b} for a, b in (edges or [])],
    }
    manifest_path = manifests_dir / "test.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))
    return manifest_path


def free_port() -> int:
    """Return an unused localhost port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


CORE_SURFACES: tuple[str, ...] = (
    "state",
    "processes",
    "metrics",
    "audit_query",
    "set_manifest",
    "reload_manifest",
    "spawn",
    "stop",
    "restart",
    "reconcile",
    "drain",
)


def core_edges_for(node_id: str, surfaces: Iterable[str] = CORE_SURFACES) -> list[tuple[str, str]]:
    """Generate ``(node_id -> core.<surface>)`` edges for the given surfaces."""
    return [(node_id, f"core.{s}") for s in surfaces]
