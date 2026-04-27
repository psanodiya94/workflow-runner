"""Connection abstractions for remote command execution."""

from workflow_runner.connection.base import (
    Connection,
    ConnectionError,
    ConnectionState,
    OnChunk,
)
from workflow_runner.connection.local import LocalConnection
from workflow_runner.connection.manager import ConnectionConfig, SessionManager
from workflow_runner.connection.ssh import SSHConnection

__all__ = [
    "Connection",
    "ConnectionConfig",
    "ConnectionError",
    "ConnectionState",
    "LocalConnection",
    "OnChunk",
    "SessionManager",
    "SSHConnection",
]
