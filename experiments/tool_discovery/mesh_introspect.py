"""mesh_introspect — discover the mesh topology and per-surface JSON Schemas.

Two modes:
  - manifest mode: parse a manifest YAML + its referenced schema files
    directly off disk. Works without a live Core; great for tests and CI.
  - live mode: query Core's /v0/admin/state endpoint with an admin token.
    Returns the schemas Core actually loaded — what the live mesh enforces.

The voice_actor seed pattern uses /v0/introspect (which omits schemas).
Schemas are the missing ingredient for *typed* tool composition, so we use
the admin endpoint or the manifest directly. The composer treats either
source as equivalent — they yield the same MeshSurface dataclass.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Optional

import urllib.request
import urllib.error

import yaml


@dataclasses.dataclass(frozen=True)
class MeshSurface:
    node_id: str
    surface_name: str          # e.g. "create_card"
    surface_type: str          # "tool" | "inbox"
    invocation_mode: str       # "request_response" | "fire_and_forget"
    schema: dict               # JSON Schema (draft-07-ish)

    @property
    def address(self) -> str:
        return f"{self.node_id}.{self.surface_name}"


@dataclasses.dataclass
class MeshTopology:
    nodes: dict[str, dict]                   # node_id -> {kind, runtime, metadata, surfaces:list[MeshSurface]}
    edges: list[tuple[str, str]]             # (from_node, to_surface_address)
    surfaces_by_address: dict[str, MeshSurface]

    def edges_from(self, node_id: str) -> list[tuple[str, str]]:
        return [(f, t) for f, t in self.edges if f == node_id]

    def reachable_surfaces_from(self, node_id: str) -> list[MeshSurface]:
        """Return MeshSurface objects this node has an allow-edge to."""
        out = []
        for f, t in self.edges:
            if f != node_id:
                continue
            s = self.surfaces_by_address.get(t)
            if s is not None:
                out.append(s)
        return out


# ---------- manifest mode ----------

def load_from_manifest(manifest_path: str | pathlib.Path) -> MeshTopology:
    """Parse the manifest YAML and load every referenced schema file.

    Returns a MeshTopology with full schemas attached.
    """
    p = pathlib.Path(manifest_path).resolve()
    text = p.read_text()
    data = yaml.safe_load(text)
    nodes: dict[str, dict] = {}
    surfaces_by_address: dict[str, MeshSurface] = {}
    for node in data.get("nodes", []):
        nid = node["id"]
        surfaces: list[MeshSurface] = []
        for s in node.get("surfaces", []):
            schema_path = (p.parent / s["schema"]).resolve()
            try:
                schema = json.loads(schema_path.read_text())
            except Exception:
                # An unreadable schema means we can't introspect — record
                # an empty object and let the LLM handle "unknown payload".
                schema = {"type": "object", "additionalProperties": True,
                          "_load_error": True}
            surf = MeshSurface(
                node_id=nid,
                surface_name=s["name"],
                surface_type=s["type"],
                invocation_mode=s.get("invocation_mode", "request_response"),
                schema=schema,
            )
            surfaces.append(surf)
            surfaces_by_address[surf.address] = surf
        nodes[nid] = {
            "kind": node.get("kind", "node"),
            "runtime": node.get("runtime", "local-process"),
            "metadata": node.get("metadata", {}),
            "surfaces": surfaces,
        }
    edges: list[tuple[str, str]] = []
    for rel in data.get("relationships", []):
        edges.append((rel["from"], rel["to"]))
    return MeshTopology(nodes=nodes, edges=edges, surfaces_by_address=surfaces_by_address)


# ---------- live mode ----------

def load_from_core(core_url: str, admin_token: str) -> MeshTopology:
    """Query a running Core's /v0/admin/state and build a MeshTopology.

    Schemas come back inline in the admin payload — we don't need disk access.
    """
    url = core_url.rstrip("/") + "/v0/admin/state"
    req = urllib.request.Request(url, headers={"X-Admin-Token": admin_token})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode())
    nodes: dict[str, dict] = {}
    surfaces_by_address: dict[str, MeshSurface] = {}
    for n in data.get("nodes", []):
        nid = n["id"]
        surfaces: list[MeshSurface] = []
        for s in n.get("surfaces", []):
            surf = MeshSurface(
                node_id=nid,
                surface_name=s["name"],
                surface_type=s["type"],
                invocation_mode=s.get("invocation_mode", "request_response"),
                schema=s.get("schema") or {"type": "object", "additionalProperties": True},
            )
            surfaces.append(surf)
            surfaces_by_address[surf.address] = surf
        nodes[nid] = {
            "kind": n.get("kind", "node"),
            "runtime": n.get("runtime", "local-process"),
            "metadata": n.get("metadata", {}),
            "surfaces": surfaces,
        }
    edges: list[tuple[str, str]] = []
    for rel in data.get("relationships", []):
        edges.append((rel["from"], rel["to"]))
    return MeshTopology(nodes=nodes, edges=edges, surfaces_by_address=surfaces_by_address)


def try_live_then_manifest(core_url: Optional[str], admin_token: Optional[str],
                           manifest_path: str | pathlib.Path) -> tuple[MeshTopology, str]:
    """Try the live Core first, then fall back to manifest mode.

    Returns (topology, source) where source is "live" or "manifest".
    """
    if core_url and admin_token:
        try:
            return load_from_core(core_url, admin_token), "live"
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
    return load_from_manifest(manifest_path), "manifest"


# ---------- composer-facing API ----------

def discover_capabilities(node_id: str, core_url: str, admin_token: str) -> list[dict]:
    """List every surface ``node_id`` is allowed to invoke, with full schemas.

    Returns a list of dicts shaped:

        {
          "target_node":   "kanban_node",
          "surface":       "create_card",
          "address":       "kanban_node.create_card",
          "surface_type":  "tool",
          "invocation_mode": "request_response",
          "schema_url":    "<core>/v0/admin/state#/nodes/kanban_node/surfaces/.../schema",
          "schema_dict":   {... raw JSON Schema ...},
        }

    The composer turns each entry into one OpenAI ``function`` tool. The
    ``schema_url`` is informational — ``schema_dict`` is the source of truth.
    """
    topo = load_from_core(core_url, admin_token)
    out: list[dict] = []
    for surf in topo.reachable_surfaces_from(node_id):
        out.append({
            "target_node": surf.node_id,
            "surface": surf.surface_name,
            "address": surf.address,
            "surface_type": surf.surface_type,
            "invocation_mode": surf.invocation_mode,
            "schema_url": (
                f"{core_url.rstrip('/')}/v0/admin/state"
                f"#/nodes/{surf.node_id}/surfaces/{surf.surface_name}/schema"
            ),
            "schema_dict": surf.schema,
        })
    return out
