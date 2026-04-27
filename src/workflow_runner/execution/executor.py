"""High-level command execution that wraps the connection layer.

The executor is intentionally transport-agnostic: it accepts any
:class:`~workflow_runner.connection.base.Connection` and orchestrates the
streaming, timeout, and security checks around it. This keeps the SSH details
contained in ``connection.ssh`` and lets us drop in alternate transports
(local subprocess, mock) for tests.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Callable, Protocol

from workflow_runner.connection.base import Connection, ConnectionError
from workflow_runner.execution.result import CommandResult, ExecutionStatus
from workflow_runner.logging_utils import get_logger
from workflow_runner.security.guard import (
    DestructiveCommandError,
    SecurityGuard,
    SecurityVerdict,
    Severity,
)


class StreamHandler(Protocol):
    """Callback invoked as each chunk of output arrives.

    ``stream`` is one of ``"stdout"`` / ``"stderr"``. Implementations must be
    thread-safe; the CLI uses a Rich console lock, the workflow engine
    appends to a buffer.
    """

    def __call__(self, stream: str, data: str) -> None: ...


ConfirmCallback = Callable[[str, SecurityVerdict], bool]


class CommandExecutor:
    """Run individual commands against an open connection."""

    def __init__(
        self,
        connection: Connection,
        *,
        guard: SecurityGuard | None = None,
        default_timeout: float | None = None,
        confirm: ConfirmCallback | None = None,
        logger_context: dict[str, str] | None = None,
    ) -> None:
        self._connection = connection
        self._guard = guard or SecurityGuard()
        self._default_timeout = default_timeout
        self._confirm = confirm
        self._log = get_logger(
            "workflow_runner.executor",
            **(logger_context or {"host": connection.describe()}),
        )

    @property
    def connection(self) -> Connection:
        return self._connection

    def run(
        self,
        command: str,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        stream: StreamHandler | None = None,
        skip_security_check: bool = False,
    ) -> CommandResult:
        """Execute ``command`` synchronously and return a :class:`CommandResult`."""
        cleaned = SecurityGuard.validate_command(command)
        verdict = SecurityVerdict(Severity.SAFE)
        if not skip_security_check:
            try:
                verdict = self._guard.assert_allowed(cleaned)
            except DestructiveCommandError as exc:
                self._log.warning(
                    "command blocked by policy",
                    extra={"command": cleaned, "rules": exc.verdict.matched_rules},
                )
                result = CommandResult(
                    command=cleaned,
                    status=ExecutionStatus.BLOCKED,
                    error=str(exc),
                )
                result.mark_finished()
                return result
            if verdict.requires_confirmation and self._confirm is not None:
                if not self._confirm(cleaned, verdict):
                    self._log.info(
                        "command aborted by user",
                        extra={"command": cleaned, "rules": verdict.matched_rules},
                    )
                    result = CommandResult(
                        command=cleaned,
                        status=ExecutionStatus.ABORTED,
                        error="user declined confirmation",
                    )
                    result.mark_finished()
                    return result

        effective_timeout = timeout if timeout is not None else self._default_timeout
        result = CommandResult(command=cleaned, metadata={"severity": verdict.severity.value})
        if env:
            result.metadata["env_keys"] = ",".join(sorted(env.keys()))
        if cwd:
            result.metadata["cwd"] = cwd

        self._log.info(
            "running command",
            extra={"command": cleaned, "timeout": effective_timeout, "cwd": cwd},
        )

        try:
            self._stream_command(
                cleaned,
                result,
                env=env,
                cwd=cwd,
                stream=stream,
                timeout=effective_timeout,
            )
        except ConnectionError as exc:
            result.error = f"connection error: {exc}"
            result.mark_finished(ExecutionStatus.FAILURE)
            self._log.error("connection error", extra={"command": cleaned, "error": str(exc)})
            return result
        except TimeoutError as exc:
            result.error = str(exc) or f"timed out after {effective_timeout}s"
            result.mark_finished(ExecutionStatus.TIMEOUT)
            self._log.warning("command timed out", extra={"command": cleaned})
            return result

        if result.exit_code == 0:
            result.mark_finished(ExecutionStatus.SUCCESS)
        else:
            result.mark_finished(ExecutionStatus.FAILURE)
        self._log.info(
            "command finished",
            extra={
                "command": cleaned,
                "exit_code": result.exit_code,
                "duration": round(result.duration, 4),
                "status": result.status.value,
            },
        )
        return result

    def run_many(
        self,
        commands: Iterable[str],
        *,
        stop_on_error: bool = True,
        **kwargs,
    ) -> list[CommandResult]:
        """Convenience for sequential execution."""
        results: list[CommandResult] = []
        for cmd in commands:
            result = self.run(cmd, **kwargs)
            results.append(result)
            if stop_on_error and not result.succeeded:
                break
        return results

    # ------------------------------------------------------------------ helpers

    def _stream_command(
        self,
        command: str,
        result: CommandResult,
        *,
        env: dict[str, str] | None,
        cwd: str | None,
        stream: StreamHandler | None,
        timeout: float | None,
    ) -> None:
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        lock = threading.Lock()

        def on_chunk(channel: str, data: str) -> None:
            with lock:
                (stdout_chunks if channel == "stdout" else stderr_chunks).append(data)
            if stream is not None:
                stream(channel, data)

        deadline = (time.time() + timeout) if timeout else None
        exit_code = self._connection.exec_command(
            command,
            on_chunk=on_chunk,
            env=env,
            cwd=cwd,
            deadline=deadline,
        )
        result.exit_code = exit_code
        result.stdout = "".join(stdout_chunks)
        result.stderr = "".join(stderr_chunks)
