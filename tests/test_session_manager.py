"""Tests for the SessionManager and ConnectionConfig."""

from __future__ import annotations

import pytest

from workflow_runner.connection.manager import ConnectionConfig, SessionManager


def test_local_session_round_trip() -> None:
    sm = SessionManager()
    conn = sm.add(ConnectionConfig(name="local", local=True), connect=True)
    assert conn.is_alive()
    assert "local" in sm.names()
    status = sm.status()
    assert status["local"]["alive"] == "yes"
    sm.remove("local")
    assert "local" not in sm.names()


def test_duplicate_name_rejected() -> None:
    sm = SessionManager()
    sm.add(ConnectionConfig(name="x", local=True), connect=True)
    with pytest.raises(ValueError):
        sm.add(ConnectionConfig(name="x", local=True), connect=True)


def test_get_unknown_raises() -> None:
    sm = SessionManager()
    with pytest.raises(KeyError):
        sm.get("missing")


def test_ssh_config_requires_host_and_user() -> None:
    cfg = ConnectionConfig(name="bad")  # not local, no host/user
    with pytest.raises(ValueError):
        cfg.build()
