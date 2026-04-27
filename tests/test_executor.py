"""Tests for the command execution engine."""

from __future__ import annotations

import pytest

from workflow_runner.execution.executor import CommandExecutor
from workflow_runner.execution.result import ExecutionStatus
from workflow_runner.security.guard import SecurityGuard


def test_run_success(fake_connection_factory) -> None:
    conn = fake_connection_factory(("hello\n", "", 0))
    executor = CommandExecutor(conn)
    result = executor.run("echo hello")
    assert result.status is ExecutionStatus.SUCCESS
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.duration >= 0


def test_run_failure(fake_connection_factory) -> None:
    conn = fake_connection_factory(("", "boom\n", 2))
    executor = CommandExecutor(conn)
    result = executor.run("false")
    assert result.status is ExecutionStatus.FAILURE
    assert result.exit_code == 2
    assert result.stderr == "boom\n"


def test_blocked_command_returns_blocked(fake_connection_factory) -> None:
    conn = fake_connection_factory()
    executor = CommandExecutor(conn, guard=SecurityGuard())
    result = executor.run("rm -rf /")
    assert result.status is ExecutionStatus.BLOCKED
    assert "rm -rf /" in result.command
    assert not conn.commands  # nothing was sent over the wire


def test_dangerous_command_aborted_when_user_declines(fake_connection_factory) -> None:
    conn = fake_connection_factory()
    executor = CommandExecutor(
        conn,
        guard=SecurityGuard(),
        confirm=lambda command, verdict: False,
    )
    result = executor.run("rm -rf /tmp/foo")
    assert result.status is ExecutionStatus.ABORTED
    assert not conn.commands


def test_dangerous_command_proceeds_when_confirmed(fake_connection_factory) -> None:
    conn = fake_connection_factory(("", "", 0))
    executor = CommandExecutor(
        conn,
        guard=SecurityGuard(),
        confirm=lambda command, verdict: True,
    )
    result = executor.run("rm -rf /tmp/foo")
    assert result.status is ExecutionStatus.SUCCESS
    assert conn.commands == ["rm -rf /tmp/foo"]


def test_streaming_callback_invoked(fake_connection_factory) -> None:
    conn = fake_connection_factory(("part1\n", "warn\n", 0))
    chunks: list[tuple[str, str]] = []
    executor = CommandExecutor(conn)
    executor.run("noop", stream=lambda channel, data: chunks.append((channel, data)))
    assert ("stdout", "part1\n") in chunks
    assert ("stderr", "warn\n") in chunks


def test_validate_command_rejects_blank(fake_connection_factory) -> None:
    conn = fake_connection_factory()
    executor = CommandExecutor(conn)
    with pytest.raises(ValueError):
        executor.run("   ")
