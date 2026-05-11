"""Tests for the TOML config loader.

Covers the precedence chain (defaults -> TOML -> env -> CLI), validation
(replay-window clamp, type-mismatch fallback), and tolerant parsing
(unknown keys/sections logged but not fatal).
"""
from __future__ import annotations

import argparse
import logging

import pytest

from core.config import (
    Config,
    REPLAY_WINDOW_DEFAULT_S,
    REPLAY_WINDOW_MAX_S,
    REPLAY_WINDOW_MIN_S,
    dump_config_toml,
    load_config,
)


def _ns(**overrides) -> argparse.Namespace:
    """Build a Namespace with every CLI-mapped attr defaulting to None."""
    base = {
        "manifest": None,
        "host": None,
        "port": None,
        "audit_log": None,
        "supervisor": None,
        "supervisor_log_dir": None,
        "auto_reconcile": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _write(tmp_path, body: str):
    p = tmp_path / "mesh.toml"
    p.write_text(body)
    return str(p)


def test_no_toml_no_env_returns_defaults():
    cfg = load_config(toml_path=None, env={})
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8000
    assert cfg.server.manifest_path is None
    assert cfg.server.invoke_timeout_s == 30
    assert cfg.admin.rate_limit == 60.0
    assert cfg.admin.rate_burst == 20.0
    assert cfg.security.replay_window_s == REPLAY_WINDOW_DEFAULT_S
    assert cfg.supervisor.enabled is False
    assert cfg.supervisor.log_dir == ".logs"
    assert cfg.supervisor.auto_reconcile is False
    assert cfg.logging.audit_log_path == "audit.log"
    # Every field's source is recorded as 'defaults'.
    assert all(v == "defaults" for v in cfg.sources.values())
    assert cfg.toml_path is None


def test_toml_values_used_when_present(tmp_path):
    path = _write(tmp_path, """
[server]
host = "0.0.0.0"
port = 9001
invoke_timeout_s = 45

[admin]
rate_limit = 120
rate_burst = 40

[security]
replay_window_s = 90

[supervisor]
enabled = true
log_dir = "/var/log/mesh"
auto_reconcile = true

[logging]
audit_log_path = "/tmp/audit.log"
""")
    cfg = load_config(toml_path=path, env={})
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9001
    assert cfg.server.invoke_timeout_s == 45
    assert cfg.admin.rate_limit == 120
    assert cfg.admin.rate_burst == 40
    assert cfg.security.replay_window_s == 90
    assert cfg.supervisor.enabled is True
    assert cfg.supervisor.log_dir == "/var/log/mesh"
    assert cfg.supervisor.auto_reconcile is True
    assert cfg.logging.audit_log_path == "/tmp/audit.log"
    assert cfg.toml_path == path
    assert cfg.sources["server.host"] == path
    assert cfg.sources["server.manifest_path"] == "defaults"  # not in toml


def test_env_overrides_toml(tmp_path):
    path = _write(tmp_path, """
[server]
host = "0.0.0.0"
port = 9001
""")
    cfg = load_config(
        toml_path=path,
        env={"MESH_HOST": "10.0.0.1", "MESH_PORT": "9500"},
    )
    assert cfg.server.host == "10.0.0.1"
    assert cfg.server.port == 9500
    assert cfg.sources["server.host"] == "env MESH_HOST"
    assert cfg.sources["server.port"] == "env MESH_PORT"


def test_cli_overrides_env(tmp_path):
    path = _write(tmp_path, "[server]\nhost = \"0.0.0.0\"\n")
    cfg = load_config(
        toml_path=path,
        env={"MESH_HOST": "10.0.0.1", "MESH_PORT": "9500"},
        cli_args=_ns(host="172.16.0.1", port=9000),
    )
    assert cfg.server.host == "172.16.0.1"
    assert cfg.server.port == 9000
    assert cfg.sources["server.host"] == "CLI --host"
    assert cfg.sources["server.port"] == "CLI --port"


def test_replay_window_clamped_high_with_warning(tmp_path, caplog):
    path = _write(tmp_path, "[security]\nreplay_window_s = 1000\n")
    with caplog.at_level(logging.WARNING, logger="mesh.config"):
        cfg = load_config(toml_path=path, env={})
    assert cfg.security.replay_window_s == REPLAY_WINDOW_MAX_S
    assert any("above ceiling" in rec.message for rec in caplog.records)


def test_replay_window_clamped_low_with_warning(tmp_path, caplog):
    path = _write(tmp_path, "[security]\nreplay_window_s = 0\n")
    with caplog.at_level(logging.WARNING, logger="mesh.config"):
        cfg = load_config(toml_path=path, env={})
    assert cfg.security.replay_window_s == REPLAY_WINDOW_MIN_S
    assert any("below floor" in rec.message for rec in caplog.records)


def test_port_wrong_type_falls_back_with_warning(tmp_path, caplog):
    path = _write(tmp_path, '[server]\nport = "not_a_number"\n')
    with caplog.at_level(logging.WARNING, logger="mesh.config"):
        cfg = load_config(toml_path=path, env={})
    assert cfg.server.port == 8000  # default preserved
    assert cfg.sources["server.port"] == "defaults"
    assert any("wrong type" in rec.message for rec in caplog.records)


def test_missing_section_uses_defaults(tmp_path):
    # Only [server] present — [admin], [security], [supervisor], [logging] absent.
    path = _write(tmp_path, '[server]\nhost = "1.2.3.4"\n')
    cfg = load_config(toml_path=path, env={})
    assert cfg.server.host == "1.2.3.4"
    assert cfg.admin.rate_limit == 60.0  # default
    assert cfg.security.replay_window_s == REPLAY_WINDOW_DEFAULT_S
    assert cfg.supervisor.enabled is False
    assert cfg.logging.audit_log_path == "audit.log"


def test_unknown_key_logged_not_fatal(tmp_path, caplog):
    path = _write(tmp_path, """
[server]
host = "1.2.3.4"
nonexistent_field = "ignored"

[mystery_section]
foo = 1
""")
    with caplog.at_level(logging.WARNING, logger="mesh.config"):
        cfg = load_config(toml_path=path, env={})
    # Known field still applied.
    assert cfg.server.host == "1.2.3.4"
    msgs = " ".join(rec.message for rec in caplog.records)
    assert "nonexistent_field" in msgs
    assert "mystery_section" in msgs


def test_invalid_env_value_warns_and_falls_back(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="mesh.config"):
        cfg = load_config(toml_path=None, env={"MESH_PORT": "banana"})
    assert cfg.server.port == 8000
    assert any("MESH_PORT" in rec.message for rec in caplog.records)


def test_env_bool_only_one_is_true():
    cfg_true = load_config(toml_path=None, env={"MESH_SUPERVISOR": "1"})
    assert cfg_true.supervisor.enabled is True
    for falsy in ("0", "", "true", "yes", "anything"):
        cfg = load_config(toml_path=None, env={"MESH_SUPERVISOR": falsy})
        assert cfg.supervisor.enabled is False, f"{falsy!r} should be False"


def test_missing_toml_file_warns(tmp_path, caplog):
    bogus = str(tmp_path / "does_not_exist.toml")
    with caplog.at_level(logging.WARNING, logger="mesh.config"):
        cfg = load_config(toml_path=bogus, env={})
    assert cfg.toml_path is None
    assert cfg.server.host == "127.0.0.1"
    assert any("does not exist" in rec.message for rec in caplog.records)


def test_dump_config_includes_source_attribution(tmp_path):
    path = _write(tmp_path, '[server]\nport = 9001\n')
    cfg = load_config(
        toml_path=path,
        env={"MESH_HOST": "10.0.0.1"},
        cli_args=_ns(audit_log="/tmp/x.log"),
    )
    out = dump_config_toml(cfg)
    assert "[server]" in out
    assert "host = \"10.0.0.1\"  # from env MESH_HOST" in out
    assert "port = 9001  # from " + path in out
    assert "manifest_path = \"\"  # from defaults" in out
    assert "audit_log_path = \"/tmp/x.log\"  # from CLI --audit-log" in out


def test_cli_store_true_only_applies_when_present():
    # When the user does NOT pass --supervisor, argparse produces None
    # (because we set default=None on store_true). The loader must NOT
    # treat that as "set to False".
    cfg = load_config(
        toml_path=None,
        env={"MESH_SUPERVISOR": "1"},
        cli_args=_ns(supervisor=None),
    )
    assert cfg.supervisor.enabled is True
    assert cfg.sources["supervisor.enabled"] == "env MESH_SUPERVISOR"

    cfg2 = load_config(
        toml_path=None,
        env={"MESH_SUPERVISOR": "1"},
        cli_args=_ns(supervisor=True),
    )
    assert cfg2.supervisor.enabled is True
    assert cfg2.sources["supervisor.enabled"] == "CLI --supervisor"


def test_replay_window_invalid_env_does_not_apply(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="mesh.config"):
        cfg = load_config(
            toml_path=None,
            env={"MESH_REPLAY_WINDOW_S": "not_a_number"},
        )
    assert cfg.security.replay_window_s == REPLAY_WINDOW_DEFAULT_S
    assert cfg.sources["security.replay_window_s"] == "defaults"
