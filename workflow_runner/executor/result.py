"""Immutable result of a single remote command execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    execution_time: float  # seconds
    timestamp: datetime
    session_id: str

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """stdout with stderr appended when non-empty."""
        if self.stderr:
            return self.stdout + self.stderr
        return self.stdout
