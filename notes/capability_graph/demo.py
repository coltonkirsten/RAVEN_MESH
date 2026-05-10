#!/usr/bin/env python3
"""RAVEN_MESH capability-graph introspection demo.

Loads any manifest YAML and answers the queries the formal model
(``MODEL.md`` §2.3) makes possible:

    who_can(surface)            -> nodes allowed to invoke a fully-qualified surface
    what_can(node)              -> (target_node, surface) pairs the node may invoke
    is_path_authorized(a, b.s)  -> bool: direct edge exists
    reachable(a)                -> nodes transitively reachable via legitimate edges
    surfaces_of(node)           -> declared surfaces on a node
    nodes()                     -> all declared node ids

Usage:
    python3 demo.py [manifest.yaml]

Defaults to ``../../manifests/full_demo.yaml`` (the largest demo manifest).

The script is read-only: it never mutates the manifest, never contacts Core,
and does not import RAVEN_MESH source. It exists as a standalone audit tool
that takes a YAML file and produces capability-graph answers.
"""
from __future__ import annotations

import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass

import yaml


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Surface:
    node_id: str
    name: str
    type: str            # "tool" | "inbox"
    invocation_mode: str # "request_response" | "fire_and_forget"

    @property
    def fqn(self) -> str:
        return f"{self.node_id}.{self.name}"


@dataclass
class CapabilityGraph:
    """In-memory projection of a manifest's allow-edges and surfaces."""

    nodes: dict[str, dict]                  # node_id -> raw node block
    surfaces: dict[str, Surface]            # fqn -> Surface
    edges: set[tuple[str, str]]             # (from_node, to.surface) tuples

    # ---- queries ----------------------------------------------------------

    def all_nodes(self) -> list[str]:
        return sorted(self.nodes)

    def surfaces_of(self, node_id: str) -> list[Surface]:
        return sorted(
            (s for s in self.surfaces.values() if s.node_id == node_id),
            key=lambda s: s.name,
        )

    def who_can(self, surface_fqn: str) -> list[str]:
        """List of node ids that may invoke ``surface_fqn``."""
        return sorted(a for (a, t) in self.edges if t == surface_fqn)

    def what_can(self, node_id: str) -> list[tuple[str, str]]:
        """(target_node, surface_name) pairs ``node_id`` may invoke directly."""
        out: list[tuple[str, str]] = []
        for a, t in self.edges:
            if a != node_id:
                continue
            if "." not in t:
                continue
            target_node, surface_name = t.split(".", 1)
            out.append((target_node, surface_name))
        return sorted(out)

    def is_path_authorized(self, from_node: str, to_fqn: str) -> bool:
        """Does the manifest contain a direct allow-edge?"""
        return (from_node, to_fqn) in self.edges

    def reachable(self, from_node: str) -> set[str]:
        """Transitive set of nodes reachable from ``from_node`` via legitimate edges.

        This is *not* the runtime authorisation check — Core only checks the
        direct edge — but it is the auditor's question: starting at ``from_node``,
        which nodes can a single chain of legitimate invocations eventually touch?
        """
        adj: dict[str, set[str]] = defaultdict(set)
        for a, t in self.edges:
            if "." not in t:
                continue
            adj[a].add(t.split(".", 1)[0])
        seen: set[str] = set()
        stack = [from_node]
        while stack:
            n = stack.pop()
            for nxt in adj.get(n, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def ambient_authority_score(self) -> list[tuple[str, int]]:
        """Diagnostic: nodes ranked by outbound edge count.

        High scores indicate "god-node" patterns (see MODEL.md §2.4 on
        ``human_node`` in ``full_demo.yaml``).
        """
        counts: dict[str, int] = defaultdict(int)
        for a, _ in self.edges:
            counts[a] += 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def orphan_edges(self) -> list[tuple[str, str]]:
        """Edges referencing nodes/surfaces that aren't declared.

        Mesh's manifest validator catches these at load time; we report them
        anyway for audit on hand-written or in-flight manifests.
        """
        bad: list[tuple[str, str]] = []
        for a, t in self.edges:
            if a not in self.nodes:
                bad.append((a, t))
                continue
            if "." not in t:
                bad.append((a, t))
                continue
            target_node, surface_name = t.split(".", 1)
            if target_node not in self.nodes:
                bad.append((a, t))
                continue
            if t not in self.surfaces:
                bad.append((a, t))
        return sorted(bad)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load(manifest_path: str | pathlib.Path) -> CapabilityGraph:
    text = pathlib.Path(manifest_path).read_text()
    raw = yaml.safe_load(text) or {}

    nodes: dict[str, dict] = {}
    surfaces: dict[str, Surface] = {}
    for node in raw.get("nodes", []) or []:
        nid = node["id"]
        nodes[nid] = node
        for s in node.get("surfaces", []) or []:
            surf = Surface(
                node_id=nid,
                name=s["name"],
                type=s.get("type", "tool"),
                invocation_mode=s.get("invocation_mode", "request_response"),
            )
            surfaces[surf.fqn] = surf

    edges: set[tuple[str, str]] = set()
    for rel in raw.get("relationships", []) or []:
        edges.add((rel["from"], rel["to"]))

    return CapabilityGraph(nodes=nodes, surfaces=surfaces, edges=edges)


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------


_DEFAULT_MANIFEST = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "manifests"
    / "full_demo.yaml"
)


def _print_section(title: str) -> None:
    print()
    print(f"=== {title} ===")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        path = pathlib.Path(argv[1]).resolve()
    else:
        path = _DEFAULT_MANIFEST
    print(f"manifest: {path}")
    g = load(path)

    _print_section("nodes")
    for nid in g.all_nodes():
        kind = g.nodes[nid].get("kind", "?")
        n_surf = len(g.surfaces_of(nid))
        print(f"  {nid}  kind={kind}  surfaces={n_surf}")

    _print_section("orphan edges (referenced but not declared)")
    orphans = g.orphan_edges()
    if not orphans:
        print("  (none)")
    else:
        for a, t in orphans:
            print(f"  {a} -> {t}")

    _print_section("ambient authority (outbound edge counts)")
    for nid, count in g.ambient_authority_score():
        print(f"  {count:3d}  {nid}")

    # Pick a target surface that exists in this manifest, for the demo query.
    sample_surface = None
    for fqn in (
        "kanban_node.create_card",
        "voice_actor.say",
        "tasks.create",
        "mesh_db_node.query",
        "webui_node.show_message",
    ):
        if fqn in g.surfaces:
            sample_surface = fqn
            break

    if sample_surface:
        _print_section(f"who_can({sample_surface!r})")
        callers = g.who_can(sample_surface)
        if not callers:
            print("  (no node has this edge)")
        else:
            for c in callers:
                print(f"  {c}")

    # Pick a sample caller for what_can / reachable.
    sample_caller = None
    for candidate in ("nexus_agent", "human_node", "voice_actor", "demo_actor", "dummy_actor"):
        if candidate in g.nodes:
            sample_caller = candidate
            break
    if sample_caller is None and g.nodes:
        sample_caller = next(iter(g.all_nodes()))

    if sample_caller:
        _print_section(f"what_can({sample_caller!r})")
        for tgt, surf in g.what_can(sample_caller):
            print(f"  {tgt}.{surf}")

        _print_section(f"reachable({sample_caller!r})  [transitive]")
        for n in sorted(g.reachable(sample_caller)):
            print(f"  {n}")

    # is_path_authorized: pick something true and something false.
    if sample_caller and sample_surface:
        _print_section("is_path_authorized samples")
        true_q = (sample_caller, sample_surface)
        # invent a deliberately-bogus edge for the false case
        bogus_q = (sample_caller, "core._secret")
        print(f"  {true_q[0]!r} -> {true_q[1]!r}: {g.is_path_authorized(*true_q)}")
        print(f"  {bogus_q[0]!r} -> {bogus_q[1]!r}: {g.is_path_authorized(*bogus_q)}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
