"""Multi-session manager with reconnect support."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from workflow_runner.connection.base import Connection, ConnectionError, ConnectionState
from workflow_runner.connection.local import LocalConnection
from workflow_runner.connection.ssh import SSHConnection, SSHCredentials, SSHEndpoint
from workflow_runner.logging_utils import get_logger


@dataclass
class ConnectionConfig:
    """Declarative config for a session.

    Either an SSH endpoint+credentials *or* ``local=True`` must be set.
    """

    name: str
    host: str | None = None
    port: int = 22
    username: str | None = None
    password: str | None = None
    key_filename: str | None = None
    passphrase: str | None = None
    use_agent: bool = True
    strict_host_key_checking: bool = True
    known_hosts: str | None = None
    connect_timeout: float = 15.0
    keepalive_interval: int = 30
    local: bool = False
    extra_options: dict[str, str] = field(default_factory=dict)

    def build(self) -> Connection:
        if self.local:
            return LocalConnection()
        if not self.host or not self.username:
            raise ValueError(f"session {self.name!r}: host and username are required for SSH")
        endpoint = SSHEndpoint(
            host=self.host,
            port=self.port,
            known_hosts=self.known_hosts,
            strict_host_key_checking=self.strict_host_key_checking,
            connect_timeout=self.connect_timeout,
            keepalive_interval=self.keepalive_interval,
            extra_ssh_options=dict(self.extra_options),
        )
        creds = SSHCredentials(
            username=self.username,
            password=self.password,
            key_filename=self.key_filename,
            passphrase=self.passphrase,
            use_agent=self.use_agent,
        )
        return SSHConnection(endpoint, creds)


class SessionManager:
    """Owns one or more named :class:`Connection` instances.

    The manager is thread-safe and handles automatic reconnect (with
    exponential backoff) when a session is found dead.
    """

    def __init__(self, *, max_reconnect_attempts: int = 4, base_backoff: float = 1.0) -> None:
        self._sessions: dict[str, Connection] = {}
        self._configs: dict[str, ConnectionConfig] = {}
        self._lock = threading.RLock()
        self._max_reconnect_attempts = max_reconnect_attempts
        self._base_backoff = base_backoff
        self._log = get_logger("workflow_runner.session_manager")

    # ------------------------------------------------------------------ CRUD
    def add(self, config: ConnectionConfig, *, connect: bool = True) -> Connection:
        with self._lock:
            if config.name in self._sessions:
                raise ValueError(f"session {config.name!r} already exists")
            connection = config.build()
            self._sessions[config.name] = connection
            self._configs[config.name] = config
            self._log.info("session registered", extra={"session": config.name, "target": connection.describe()})
            if connect:
                self._connect_with_retry(config.name)
            return connection

    def get(self, name: str) -> Connection:
        with self._lock:
            try:
                return self._sessions[name]
            except KeyError:
                raise KeyError(f"unknown session: {name!r}") from None

    def remove(self, name: str) -> None:
        with self._lock:
            connection = self._sessions.pop(name, None)
            self._configs.pop(name, None)
            if connection is None:
                return
            try:
                connection.close()
            except Exception:  # pragma: no cover - close should be best effort
                self._log.debug("error closing %s", name, exc_info=True)
            self._log.info("session removed", extra={"session": name})

    def names(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    def __iter__(self) -> Iterator[tuple[str, Connection]]:
        with self._lock:
            return iter(list(self._sessions.items()))

    # --------------------------------------------------------------- queries
    def status(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {
                name: {
                    "target": conn.describe(),
                    "state": conn.state.value,
                    "alive": "yes" if conn.is_alive() else "no",
                }
                for name, conn in self._sessions.items()
            }

    # ----------------------------------------------------------- connections
    def ensure_alive(self, name: str) -> Connection:
        with self._lock:
            connection = self.get(name)
            if connection.is_alive():
                return connection
            self._log.warning("session %s appears dead; reconnecting", name)
            return self._connect_with_retry(name)

    def disconnect(self, name: str) -> None:
        with self._lock:
            self.get(name).close()

    def disconnect_all(self) -> None:
        with self._lock:
            for name in list(self._sessions):
                try:
                    self._sessions[name].close()
                except Exception:  # pragma: no cover
                    self._log.debug("error closing %s", name, exc_info=True)

    # ------------------------------------------------------------- internals
    def _connect_with_retry(self, name: str) -> Connection:
        connection = self._sessions[name]
        last_error: Exception | None = None
        for attempt in range(1, self._max_reconnect_attempts + 1):
            try:
                # SSHConnection.connect() is idempotent and supports reconnect.
                if isinstance(connection, SSHConnection) and connection.state in (
                    ConnectionState.ERROR,
                    ConnectionState.CLOSED,
                    ConnectionState.RECONNECTING,
                ):
                    connection.reconnect()
                else:
                    connection.connect()
                return connection
            except ConnectionError as exc:
                last_error = exc
                backoff = self._base_backoff * (2 ** (attempt - 1))
                self._log.warning(
                    "connect attempt %d/%d failed for %s: %s (retrying in %.1fs)",
                    attempt,
                    self._max_reconnect_attempts,
                    name,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
        assert last_error is not None
        raise last_error
