"""Shared pytest fixtures."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable
from typing import Callable

import pytest

from workflow_runner.connection.base import (
    Connection,
    ConnectionError,
    ConnectionState,
    OnChunk,
)


class FakeConnection(Connection):
    """Deterministic connection driven by a script of (stdout, stderr, exit) tuples."""

    def __init__(
        self,
        responses: Iterable[tuple[str, str, int]] | None = None,
        *,
        connect_failures: int = 0,
    ) -> None:
        self._responses: deque[tuple[str, str, int]] = deque(responses or [])
        self._state = ConnectionState.DISCONNECTED
        self._connect_failures = connect_failures
        self.commands: list[str] = []
        self.connect_calls = 0

    @property
    def state(self) -> ConnectionState:
        return self._state

    def describe(self) -> str:
        return "fake://test"

    def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_failures > 0:
            self._connect_failures -= 1
            self._state = ConnectionState.ERROR
            raise ConnectionError("simulated connect failure")
        self._state = ConnectionState.CONNECTED

    def close(self) -> None:
        self._state = ConnectionState.CLOSED

    def is_alive(self) -> bool:
        return self._state is ConnectionState.CONNECTED

    def exec_command(
        self,
        command: str,
        *,
        on_chunk: OnChunk,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        deadline: float | None = None,
    ) -> int:
        self.commands.append(command)
        if not self._responses:
            on_chunk("stdout", "")
            return 0
        stdout, stderr, code = self._responses.popleft()
        if stdout:
            on_chunk("stdout", stdout)
        if stderr:
            on_chunk("stderr", stderr)
        if deadline is not None and time.time() > deadline:
            raise TimeoutError("command timed out")
        return code


@pytest.fixture
def fake_connection_factory() -> Callable[..., FakeConnection]:
    def _factory(*responses: tuple[str, str, int], connect_failures: int = 0) -> FakeConnection:
        conn = FakeConnection(responses=responses, connect_failures=connect_failures)
        conn.connect()
        return conn

    return _factory
