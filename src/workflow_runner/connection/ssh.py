"""SSH transport built on paramiko.

The implementation is intentionally minimal: one paramiko ``SSHClient`` per
:class:`SSHConnection`, persistent across many :meth:`exec_command` calls,
with a transport keepalive so dropped TCP sessions are detected promptly.

Each command runs in its own paramiko channel — that's the simplest model
that supports reliable per-command exit codes and stdout/stderr separation.
A future enhancement (tracked in the README extensibility section) is to
back this with an interactive shell channel for stateful sessions.
"""

from __future__ import annotations

import os
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import paramiko
except ImportError as exc:  # pragma: no cover - paramiko is a hard dep
    raise ImportError(
        "paramiko is required for SSH connections. Install with `pip install paramiko`."
    ) from exc

from workflow_runner.connection.base import (
    Connection,
    ConnectionError,
    ConnectionState,
    OnChunk,
)
from workflow_runner.logging_utils import get_logger

_DEFAULT_BANNER_TIMEOUT = 30.0
_DEFAULT_AUTH_TIMEOUT = 30.0
_DEFAULT_KEEPALIVE = 30  # seconds
_CHUNK_SIZE = 8192


@dataclass
class SSHCredentials:
    """Authentication material for an SSH connection.

    Exactly one of ``key_filename`` / ``pkey`` / ``password`` /
    ``use_agent`` must be provided. ``passphrase`` is only used for encrypted
    private keys and is never logged.
    """

    username: str
    password: str | None = None
    key_filename: str | None = None
    pkey: paramiko.PKey | None = None
    passphrase: str | None = None
    use_agent: bool = True
    allow_agent_forwarding: bool = False

    def has_any_credential(self) -> bool:
        return any([self.password, self.key_filename, self.pkey, self.use_agent])


@dataclass
class SSHEndpoint:
    host: str
    port: int = 22
    known_hosts: str | None = None  # None -> ~/.ssh/known_hosts
    strict_host_key_checking: bool = True
    connect_timeout: float = 15.0
    keepalive_interval: int = _DEFAULT_KEEPALIVE
    extra_ssh_options: dict[str, str] = field(default_factory=dict)


class SSHConnection(Connection):
    """Persistent SSH session that streams command output back to the caller."""

    def __init__(self, endpoint: SSHEndpoint, credentials: SSHCredentials) -> None:
        if not credentials.has_any_credential():
            raise ValueError("no SSH credentials provided (password / key / agent)")
        self._endpoint = endpoint
        self._creds = credentials
        self._client: paramiko.SSHClient | None = None
        self._state = ConnectionState.DISCONNECTED
        self._lock = threading.RLock()
        self._log = get_logger(
            "workflow_runner.connection.ssh",
            host=endpoint.host,
            port=endpoint.port,
            user=credentials.username,
        )

    # ------------------------------------------------------------- properties
    @property
    def state(self) -> ConnectionState:
        return self._state

    def describe(self) -> str:
        return f"{self._creds.username}@{self._endpoint.host}:{self._endpoint.port}"

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> None:
        with self._lock:
            if self._state is ConnectionState.CONNECTED and self.is_alive():
                return
            self._state = ConnectionState.CONNECTING
            client = paramiko.SSHClient()

            if self._endpoint.known_hosts is None:
                default_known = Path.home() / ".ssh" / "known_hosts"
                if default_known.exists():
                    try:
                        client.load_host_keys(str(default_known))
                    except OSError as exc:
                        self._log.warning("could not load known_hosts: %s", exc)
            else:
                client.load_host_keys(self._endpoint.known_hosts)

            if self._endpoint.strict_host_key_checking:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                self._log.warning("strict host key checking disabled")
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            try:
                client.connect(
                    hostname=self._endpoint.host,
                    port=self._endpoint.port,
                    username=self._creds.username,
                    password=self._creds.password,
                    key_filename=self._creds.key_filename,
                    pkey=self._creds.pkey,
                    passphrase=self._creds.passphrase,
                    allow_agent=self._creds.use_agent,
                    look_for_keys=self._creds.use_agent,
                    timeout=self._endpoint.connect_timeout,
                    banner_timeout=_DEFAULT_BANNER_TIMEOUT,
                    auth_timeout=_DEFAULT_AUTH_TIMEOUT,
                )
            except paramiko.AuthenticationException as exc:
                self._state = ConnectionState.ERROR
                raise ConnectionError(f"authentication failed for {self.describe()}") from exc
            except (paramiko.SSHException, OSError) as exc:
                self._state = ConnectionState.ERROR
                raise ConnectionError(
                    f"failed to connect to {self.describe()}: {exc}"
                ) from exc

            transport = client.get_transport()
            if transport is not None and self._endpoint.keepalive_interval:
                transport.set_keepalive(self._endpoint.keepalive_interval)

            self._client = client
            self._state = ConnectionState.CONNECTED
            self._log.info("connected")

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # pragma: no cover - best effort
                    self._log.debug("error while closing client", exc_info=True)
                self._client = None
            self._state = ConnectionState.CLOSED
            self._log.info("disconnected")

    def is_alive(self) -> bool:
        with self._lock:
            if self._client is None:
                return False
            transport = self._client.get_transport()
            return bool(transport and transport.is_active())

    def reconnect(self) -> None:
        """Tear down and re-establish the underlying SSH session."""
        with self._lock:
            self._log.info("reconnecting")
            self._state = ConnectionState.RECONNECTING
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # pragma: no cover
                    pass
                self._client = None
            self.connect()

    # ------------------------------------------------------------- execution
    def exec_command(
        self,
        command: str,
        *,
        on_chunk: OnChunk,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        deadline: float | None = None,
    ) -> int:
        if not self.is_alive():
            raise ConnectionError("SSH connection is not active; call connect() first")

        prepared = _prepare_command(command, env=env, cwd=cwd)

        assert self._client is not None
        transport = self._client.get_transport()
        if transport is None:
            raise ConnectionError("SSH transport is not available")

        try:
            channel = transport.open_session()
        except paramiko.SSHException as exc:
            raise ConnectionError(f"could not open channel: {exc}") from exc

        channel.set_combine_stderr(False)
        channel.get_pty(term=os.environ.get("TERM", "xterm-256color"))
        channel.exec_command(prepared)

        try:
            return _drain_channel(channel, on_chunk=on_chunk, deadline=deadline)
        finally:
            try:
                channel.close()
            except Exception:  # pragma: no cover - best effort
                pass


def _prepare_command(
    command: str,
    *,
    env: dict[str, str] | None,
    cwd: str | None,
) -> str:
    """Turn a command + env + cwd into a single shell-safe one-liner.

    We can't rely on ``channel.update_environment`` because ``AcceptEnv`` is
    disabled on most sshd configs. Encoding via ``env`` / ``cd`` is portable
    and explicit.
    """
    parts: list[str] = ["set -o pipefail"]
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)}")
    if env:
        for key, value in env.items():
            if not _is_valid_env_key(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
            parts.append(f"export {key}={shlex.quote(value)}")
    parts.append(command)
    return "bash -c " + shlex.quote(" && ".join(parts))


def _is_valid_env_key(key: str) -> bool:
    if not key or key[0].isdigit():
        return False
    return all(c.isalnum() or c == "_" for c in key)


def _drain_channel(
    channel: paramiko.Channel,
    *,
    on_chunk: OnChunk,
    deadline: float | None,
) -> int:
    """Stream stdout/stderr until the remote command finishes."""
    stdout_buf = bytearray()
    stderr_buf = bytearray()

    def _flush_lines(buf: bytearray, stream: str) -> None:
        # Emit complete lines as they appear, but keep partial trailing data.
        if not buf:
            return
        try:
            decoded = buf.decode("utf-8", errors="replace")
        except UnicodeDecodeError:  # pragma: no cover - errors=replace prevents this
            return
        on_chunk(stream, decoded)
        buf.clear()

    while True:
        if deadline is not None and time.time() > deadline:
            try:
                channel.close()
            finally:
                _flush_lines(stdout_buf, "stdout")
                _flush_lines(stderr_buf, "stderr")
            raise TimeoutError("command timed out")

        progressed = False
        if channel.recv_ready():
            chunk = channel.recv(_CHUNK_SIZE)
            if chunk:
                stdout_buf.extend(chunk)
                _flush_lines(stdout_buf, "stdout")
                progressed = True
        if channel.recv_stderr_ready():
            chunk = channel.recv_stderr(_CHUNK_SIZE)
            if chunk:
                stderr_buf.extend(chunk)
                _flush_lines(stderr_buf, "stderr")
                progressed = True

        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break

        if not progressed:
            time.sleep(0.02)

    # Drain any final bytes that arrived after exit_status was reported.
    while channel.recv_ready():
        stdout_buf.extend(channel.recv(_CHUNK_SIZE))
    while channel.recv_stderr_ready():
        stderr_buf.extend(channel.recv_stderr(_CHUNK_SIZE))
    _flush_lines(stdout_buf, "stdout")
    _flush_lines(stderr_buf, "stderr")
    return channel.recv_exit_status()
