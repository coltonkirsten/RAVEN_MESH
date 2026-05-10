"""Tests for ``core.manifest_validator.validate_manifest``.

Each test builds a manifest dict in-memory and writes any referenced surface
schema files into the test's tmp_path so the validator can resolve them.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.manifest_validator import validate_manifest  # noqa: E402


# ---- helpers ---------------------------------------------------------------


def _write_schema(dir_path: pathlib.Path, name: str, body: dict | str | None = None) -> str:
    """Write a permissive surface schema and return the path relative to dir_path."""
    full = dir_path / name
    full.parent.mkdir(parents=True, exist_ok=True)
    if body is None:
        body = {"$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object", "additionalProperties": True}
    if isinstance(body, dict):
        full.write_text(json.dumps(body))
    else:
        full.write_text(body)
    return name


def _node(nid: str, *, kind: str = "capability", surfaces=None,
          identity_secret: str | None = None) -> dict:
    out: dict = {
        "id": nid,
        "kind": kind,
        "runtime": "local-process",
        "surfaces": surfaces or [],
    }
    if identity_secret is not None:
        out["identity_secret"] = identity_secret
    return out


def _surface(name: str, schema_path: str, *, type_: str = "tool",
             invocation_mode: str = "request_response") -> dict:
    return {
        "name": name,
        "type": type_,
        "invocation_mode": invocation_mode,
        "schema": schema_path,
    }


# ---- happy path ------------------------------------------------------------


def test_valid_manifest_returns_no_errors_or_warnings(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [
            _node("alpha", surfaces=[_surface("ping", schema)]),
            _node("beta", surfaces=[_surface("pong", schema)]),
        ],
        "relationships": [
            {"from": "alpha", "to": "beta.pong"},
        ],
    }
    errors, warnings = validate_manifest(manifest, tmp_path,
                                          env={"NOTHING": "x"})
    assert errors == []
    assert warnings == []


def test_real_demo_manifest_passes(tmp_path):
    """The shipped demo.yaml should validate cleanly."""
    manifest_path = ROOT / "manifests" / "demo.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    # Inject all the env vars demo.yaml references so identity_secret warnings
    # don't fire.
    env = {v: "x" for v in [
        "VOICE_SECRET", "TASKS_SECRET",
        "HUMAN_APPROVAL_SECRET", "EXTERNAL_NODE_SECRET",
    ]}
    errors, _ = validate_manifest(manifest, manifest_path.parent, env=env)
    assert errors == [], errors


def test_real_full_demo_flags_undeclared_nexus_agent(tmp_path):
    """The known-bad full_demo.yaml has 10 edges from undeclared nexus_agent."""
    manifest_path = ROOT / "manifests" / "full_demo.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    errors, _ = validate_manifest(manifest, manifest_path.parent, env={})
    nexus_errs = [e for e in errors if "nexus_agent" in e and "undeclared" in e]
    assert len(nexus_errs) >= 10, (
        f"expected >=10 errors flagging undeclared nexus_agent, got: {errors}"
    )


# ---- errors ----------------------------------------------------------------


def test_relationship_from_undeclared_node(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [_node("alpha", surfaces=[_surface("ping", schema)])],
        "relationships": [{"from": "ghost", "to": "alpha.ping"}],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any("undeclared node 'ghost'" in e for e in errors), errors


def test_relationship_to_undeclared_node(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [_node("alpha", surfaces=[_surface("ping", schema)])],
        "relationships": [{"from": "alpha", "to": "missing.ping"}],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any("undeclared node 'missing'" in e for e in errors), errors


def test_relationship_to_unknown_surface(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [
            _node("alpha", surfaces=[_surface("ping", schema)]),
            _node("beta", surfaces=[_surface("pong", schema)]),
        ],
        "relationships": [{"from": "alpha", "to": "beta.nonexistent"}],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any(
        "surface 'nonexistent' does not exist on node 'beta'" in e
        for e in errors
    ), errors


def test_duplicate_node_ids(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [
            _node("alpha", surfaces=[_surface("ping", schema)]),
            _node("alpha", surfaces=[_surface("pong", schema)]),
        ],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any("duplicate node id 'alpha'" in e for e in errors), errors


def test_reserved_node_id_core(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [_node("core", surfaces=[_surface("ping", schema)])],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any("'core' is reserved" in e for e in errors), errors


def test_node_id_with_dot_or_slash(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [
            _node("bad.id", surfaces=[_surface("ping", schema)]),
            _node("worse/id", surfaces=[_surface("ping", schema)]),
        ],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    # Either schema pattern or our explicit check should flag both.
    assert any("bad.id" in e for e in errors), errors
    assert any("worse/id" in e for e in errors), errors


def test_surface_name_collision_within_node(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [
            _node("alpha", surfaces=[
                _surface("ping", schema),
                _surface("ping", schema),
            ]),
        ],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any("duplicate surface name 'ping'" in e for e in errors), errors


def test_missing_schema_file(tmp_path):
    manifest = {
        "nodes": [_node("alpha", surfaces=[_surface("ping", "nope.json")])],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any("schema file not found: nope.json" in e for e in errors), errors


def test_unparseable_schema_file(tmp_path):
    _write_schema(tmp_path, "broken.json", body="this is not json {{{")
    manifest = {
        "nodes": [_node("alpha", surfaces=[_surface("ping", "broken.json")])],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any("failed to parse" in e for e in errors), errors


def test_missing_top_level_nodes(tmp_path):
    manifest = {"relationships": []}
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any("nodes" in e and "required" in e for e in errors), errors


def test_unknown_kind_rejected(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [
            {
                "id": "alpha",
                "kind": "wizard",  # not in enum
                "runtime": "local-process",
                "surfaces": [_surface("ping", schema)],
            },
        ],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any("kind" in e for e in errors), errors


def test_relationship_to_missing_dot(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [_node("alpha", surfaces=[_surface("ping", schema)])],
        "relationships": [{"from": "alpha", "to": "no_dot_here"}],
    }
    errors, _ = validate_manifest(manifest, tmp_path, env={})
    assert any("to" in e for e in errors), errors


# ---- warnings (non-fatal) --------------------------------------------------


def test_unresolved_env_secret_warns_does_not_error(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [
            _node("alpha",
                  surfaces=[_surface("ping", schema)],
                  identity_secret="env:NOT_SET_ANYWHERE"),
        ],
    }
    errors, warnings = validate_manifest(manifest, tmp_path, env={})
    assert errors == []
    assert any("NOT_SET_ANYWHERE" in w for w in warnings), warnings


def test_resolved_env_secret_no_warning(tmp_path):
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [
            _node("alpha",
                  surfaces=[_surface("ping", schema)],
                  identity_secret="env:MY_SECRET"),
        ],
    }
    errors, warnings = validate_manifest(
        manifest, tmp_path, env={"MY_SECRET": "actual-value"}
    )
    assert errors == []
    assert warnings == []


def test_validator_never_raises_on_garbage_input(tmp_path):
    """A non-dict manifest should produce errors, not exceptions."""
    errors, warnings = validate_manifest("just a string", tmp_path, env={})
    assert errors and "mapping" in errors[0]

    errors, warnings = validate_manifest(None, tmp_path, env={})
    assert errors

    errors, warnings = validate_manifest([], tmp_path, env={})
    assert errors


def test_empty_env_secret_value_warns(tmp_path):
    """An env var that is set to empty string is treated as unresolved."""
    schema = _write_schema(tmp_path, "echo.json")
    manifest = {
        "nodes": [
            _node("alpha",
                  surfaces=[_surface("ping", schema)],
                  identity_secret="env:EMPTY_VAR"),
        ],
    }
    errors, warnings = validate_manifest(
        manifest, tmp_path, env={"EMPTY_VAR": ""}
    )
    assert errors == []
    assert any("EMPTY_VAR" in w for w in warnings), warnings
