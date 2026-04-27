"""Dataclasses describing a workflow and its steps."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum


class OnFailure(str, Enum):
    """How the engine should react when a step fails."""

    STOP = "stop"        # halt the workflow (default)
    CONTINUE = "continue"  # log the failure but keep going
    PROMPT = "prompt"    # ask the operator interactively (debugger / CLI only)


@dataclass(frozen=True)
class Step:
    """A single command in a workflow.

    The dataclass is frozen so a loaded workflow can be shared across threads.
    """

    name: str
    command: str
    description: str = ""
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    on_failure: OnFailure = OnFailure.STOP
    confirm: bool = False
    skip: bool = False
    tags: tuple[str, ...] = ()
    expect_exit_codes: tuple[int, ...] = (0,)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("step.name is required")
        if not self.command or not self.command.strip():
            raise ValueError(f"step {self.name!r}: command is required")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"step {self.name!r}: timeout must be positive")
        if not self.expect_exit_codes:
            raise ValueError(f"step {self.name!r}: expect_exit_codes must not be empty")


@dataclass(frozen=True)
class Workflow:
    """An ordered collection of :class:`Step`s plus metadata."""

    name: str
    steps: tuple[Step, ...]
    description: str = ""
    version: str = "1"
    default_cwd: str | None = None
    default_env: dict[str, str] = field(default_factory=dict)
    default_timeout: float | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("workflow.name is required")
        if not self.steps:
            raise ValueError(f"workflow {self.name!r}: must contain at least one step")
        seen: set[str] = set()
        for step in self.steps:
            if step.name in seen:
                raise ValueError(f"workflow {self.name!r}: duplicate step name {step.name!r}")
            seen.add(step.name)

    def __iter__(self) -> Iterator[Step]:
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def step_by_name(self, name: str) -> Step:
        for step in self.steps:
            if step.name == name:
                return step
        raise KeyError(name)
