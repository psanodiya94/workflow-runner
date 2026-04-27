"""Abstract base class for command-execution transports."""

from __future__ import annotations

import abc
from enum import Enum
from typing import Callable


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"
    CLOSED = "closed"


class ConnectionError(RuntimeError):
    """Raised for any transport-level failure (auth, network, channel)."""


OnChunk = Callable[[str, str], None]
"""Callback signature: ``on_chunk(stream_name, text)``."""


class Connection(abc.ABC):
    """Pluggable transport that can execute commands on a remote host.

    Implementations stream output through ``on_chunk`` so the caller never has
    to wait for the command to finish to display progress. ``deadline`` is an
    absolute monotonic-ish timestamp (``time.time()``-based) — implementations
    raise :class:`TimeoutError` if it elapses.
    """

    @property
    @abc.abstractmethod
    def state(self) -> ConnectionState: ...

    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def is_alive(self) -> bool: ...

    @abc.abstractmethod
    def describe(self) -> str:
        """Short, human-readable identifier (``user@host:port``)."""

    @abc.abstractmethod
    def exec_command(
        self,
        command: str,
        *,
        on_chunk: OnChunk,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        deadline: float | None = None,
    ) -> int:
        """Run ``command`` and return its exit code.

        ``on_chunk`` is invoked for every chunk of stdout/stderr received.
        Implementations MUST decode bytes to ``str`` before passing them on.
        """

    def __enter__(self) -> Connection:
        if self.state is not ConnectionState.CONNECTED:
            self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
