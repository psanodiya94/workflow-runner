"""Value objects describing the outcome of running a remote command."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"          # non-zero exit code
    TIMEOUT = "timeout"
    ABORTED = "aborted"          # cancelled by user / debugger stop
    SKIPPED = "skipped"          # workflow step skipped
    BLOCKED = "blocked"          # security guard rejected


@dataclass
class CommandResult:
    """A single command's outcome, including streamed output and timing."""

    command: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    status: ExecutionStatus = ExecutionStatus.SUCCESS
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    duration: float = 0.0
    error: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def mark_finished(self, status: ExecutionStatus | None = None) -> None:
        self.finished_at = time.time()
        self.duration = self.finished_at - self.started_at
        if status is not None:
            self.status = status

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.SUCCESS

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "status": self.status.value,
            "duration": round(self.duration, 4),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "metadata": dict(self.metadata),
        }
