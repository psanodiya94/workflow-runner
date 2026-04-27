"""SSH session: persistent connection with auto-reconnect and streaming execution."""

from __future__ import annotations

import select
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import paramiko

from workflow_runner.logger import get_logger


class SessionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass
class SessionConfig:
    """All parameters needed to open an SSH connection."""

    host: str
    port: int = 22
    username: str = ""
    key_path: Optional[str] = None
    # password is stored in memory only; never written to logs or files
    password: Optional[str] = None
    timeout: float = 30.0
    keepalive_interval: int = 30
    max_reconnect_attempts: int = 3
    reconnect_delay: float = 2.0

    def __post_init__(self) -> None:
        if not self.username:
            import getpass
            self.username = getpass.getuser()


class Session:
    """
    A persistent, thread-safe SSH session.

    The session stays open until :meth:`disconnect` is called.
    If the underlying transport drops unexpectedly, :meth:`execute`
    will attempt to reconnect before raising an error.
    """

    def __init__(self, session_id: str, config: SessionConfig) -> None:
        self.session_id = session_id
        self.config = config
        self.state = SessionState.DISCONNECTED
        self._client: Optional[paramiko.SSHClient] = None
        # RLock allows the same thread to re-enter (e.g. reconnect called from execute)
        self._lock = threading.RLock()
        self._connect_time: Optional[float] = None
        self._log = get_logger(f"connection.session", session_id=session_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def label(self) -> str:
        """Human-readable 'user@host:port' identifier."""
        return f"{self.config.username}@{self.config.host}:{self.config.port}"

    def connect(self) -> None:
        """Open the SSH connection. Idempotent if already connected."""
        with self._lock:
            if self.state == SessionState.CONNECTED and self.is_connected():
                return
            self._do_connect()

    def disconnect(self) -> None:
        """Close the SSH connection gracefully."""
        with self._lock:
            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
            self.state = SessionState.DISCONNECTED
            self._connect_time = None
        self._log.info("Disconnected from %s", self.config.host)

    def reconnect(self) -> bool:
        """
        Attempt to re-establish the connection up to
        ``config.max_reconnect_attempts`` times with exponential back-off.

        Returns True on success, False if all attempts are exhausted.
        """
        self._log.info("Reconnecting to %s…", self.config.host)
        with self._lock:
            self.state = SessionState.RECONNECTING
            for attempt in range(1, self.config.max_reconnect_attempts + 1):
                try:
                    self._do_connect()
                    return True
                except Exception as exc:
                    self._log.warning(
                        "Reconnect attempt %d/%d failed: %s",
                        attempt,
                        self.config.max_reconnect_attempts,
                        exc,
                    )
                    time.sleep(self.config.reconnect_delay * attempt)
            self.state = SessionState.ERROR
            return False

    def is_connected(self) -> bool:
        """Return True only when the underlying SSH transport is alive."""
        if self._client is None or self.state != SessionState.CONNECTED:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    @property
    def uptime(self) -> Optional[float]:
        """Seconds since the connection was established, or None."""
        if self._connect_time and self.state == SessionState.CONNECTED:
            return time.monotonic() - self._connect_time
        return None

    def execute(
        self,
        command: str,
        *,
        timeout: Optional[float] = None,
        env: Optional[dict[str, str]] = None,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
    ) -> "CommandResult":
        """
        Execute *command* on the remote host and return a :class:`CommandResult`.

        stdout and stderr are streamed in real time to the optional callbacks.
        The full text is also captured in the returned result.

        Raises :class:`RuntimeError` if the session cannot be (re)connected.
        """
        from datetime import datetime
        from workflow_runner.executor.result import CommandResult

        # Ensure we have a live connection before allocating a channel
        with self._lock:
            if not self.is_connected():
                if not self.reconnect():
                    raise RuntimeError(
                        f"Session '{self.session_id}' is not connected and could not reconnect"
                    )
            stdin, stdout_ch, stderr_ch = self._client.exec_command(
                command,
                timeout=timeout or self.config.timeout,
                environment=env or {},
            )

        start = time.monotonic()
        timestamp = datetime.now()
        channel = stdout_ch.channel

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        try:
            while True:
                # select gives us a non-busy wait until data or timeout
                readable, _, _ = select.select([channel], [], [], 0.05)
                if readable:
                    if channel.recv_ready():
                        chunk = channel.recv(4096).decode("utf-8", errors="replace")
                        stdout_parts.append(chunk)
                        if on_stdout:
                            on_stdout(chunk)
                    if channel.recv_stderr_ready():
                        chunk = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                        stderr_parts.append(chunk)
                        if on_stderr:
                            on_stderr(chunk)

                if channel.exit_status_ready():
                    # Drain whatever is left after the process exits
                    while channel.recv_ready():
                        chunk = channel.recv(4096).decode("utf-8", errors="replace")
                        stdout_parts.append(chunk)
                        if on_stdout:
                            on_stdout(chunk)
                    while channel.recv_stderr_ready():
                        chunk = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                        stderr_parts.append(chunk)
                        if on_stderr:
                            on_stderr(chunk)
                    break

        except Exception as exc:
            self.state = SessionState.ERROR
            self._log.error("I/O error during command execution: %s", exc)
            raise RuntimeError(f"Connection lost during command execution: {exc}") from exc

        exit_code = channel.recv_exit_status()
        elapsed = time.monotonic() - start

        result = CommandResult(
            command=command,
            exit_code=exit_code,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            execution_time=elapsed,
            timestamp=timestamp,
            session_id=self.session_id,
        )
        self._log.debug("exit=%d time=%.2fs cmd=%r", exit_code, elapsed, command)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _do_connect(self) -> None:
        """Low-level connect — must be called with self._lock held."""
        self.state = SessionState.CONNECTING
        self._log.info(
            "Connecting to %s@%s:%d", self.config.username, self.config.host, self.config.port
        )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs: dict = {
            "hostname": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
            "timeout": self.config.timeout,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if self.config.key_path:
            kwargs["key_filename"] = str(Path(self.config.key_path).expanduser())
        if self.config.password:
            kwargs["password"] = self.config.password

        client.connect(**kwargs)

        transport = client.get_transport()
        if transport:
            transport.set_keepalive(self.config.keepalive_interval)

        self._client = client
        self.state = SessionState.CONNECTED
        self._connect_time = time.monotonic()
        self._log.info("Connected to %s", self.label)
