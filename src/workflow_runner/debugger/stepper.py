"""gdb-style step-by-step driver for workflows.

The debugger sits on top of :class:`~workflow_runner.workflow.engine.WorkflowEngine`
and lets a frontend (CLI today, possibly TUI later) drive execution one step
at a time. State machine:

* The next un-executed step is the *current* step.
* :meth:`step` runs the current step and advances the cursor.
* :meth:`previous` only re-displays previous output (we can't safely "undo"
  a remote command, so this is read-only by design).
* :meth:`continue_remaining` runs all remaining steps in order.
* :meth:`stop` aborts the workflow; subsequent calls become no-ops.

The class is single-threaded — frontends call into it from one place at a
time. Its methods are pure dispatch; the actual side effects happen inside
the engine.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

from workflow_runner.execution.result import CommandResult, ExecutionStatus
from workflow_runner.workflow.engine import (
    StepEvent,
    StepEventKind,
    WorkflowEngine,
    WorkflowReport,
)


class DebuggerCommand(str, Enum):
    NEXT = "next"
    CONTINUE = "continue"
    STOP = "stop"
    PREV = "prev"


@dataclass
class _ExecutedStep:
    index: int
    name: str
    command: str
    result: CommandResult


class WorkflowDebugger:
    """Step-through controller around a :class:`WorkflowEngine`."""

    def __init__(self, engine: WorkflowEngine) -> None:
        self._engine = engine
        self._iter: Iterator[StepEvent] | None = None
        self._history: list[_ExecutedStep] = []
        self._stopped = False
        self._exhausted = False
        self._report = WorkflowReport(workflow=engine.workflow.name, total=len(engine.workflow))

    # ------------------------------------------------------------- properties
    @property
    def history(self) -> list[_ExecutedStep]:
        return list(self._history)

    @property
    def report(self) -> WorkflowReport:
        return self._report

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    @property
    def is_done(self) -> bool:
        return self._exhausted or self._stopped

    @property
    def cursor(self) -> int:
        """Index of the next step that *would* run (or len(workflow) if done)."""
        return len(self._history)

    # --------------------------------------------------------------- actions
    def step(self) -> _ExecutedStep | None:
        """Execute the current step and return the executed-step record.

        Returns ``None`` if the workflow has already finished or been stopped.
        """
        if self.is_done:
            return None
        if self._iter is None:
            self._iter = self._engine.iter_steps()

        # Drain events until we've handled exactly one step (STARTED then
        # FINISHED/SKIPPED/BLOCKED). The engine emits a STARTED event right
        # before the work, then a FINISHED event after.
        while True:
            try:
                event = next(self._iter)
            except StopIteration:
                self._exhausted = True
                return None
            if event.kind is StepEventKind.STARTED:
                continue
            assert event.result is not None
            executed = _ExecutedStep(
                index=event.index,
                name=event.step.name,
                command=event.step.command,
                result=event.result,
            )
            self._history.append(executed)
            self._report.results.append(event.result)
            WorkflowEngine._tally(self._report, event.result)  # noqa: SLF001 - intentional reuse
            if event.index + 1 >= len(self._engine.workflow):
                self._exhausted = True
            return executed

    def continue_remaining(self) -> WorkflowReport:
        """Run every remaining step until completion or a stop."""
        while not self.is_done:
            executed = self.step()
            if executed is None:
                break
            # Honour on_failure semantics: PROMPT/STOP halt; CONTINUE proceeds.
            if executed.result.status not in (ExecutionStatus.SUCCESS, ExecutionStatus.SKIPPED):
                step_def = self._engine.workflow.steps[executed.index]
                from workflow_runner.workflow.model import OnFailure

                if step_def.on_failure is OnFailure.STOP:
                    self.stop()
                    break
                if step_def.on_failure is OnFailure.PROMPT:
                    # Frontends drive PROMPT mode via single-stepping; in
                    # ``continue`` we treat it as STOP so we don't silently
                    # ignore the directive.
                    self.stop()
                    break
        return self._report

    def previous(self) -> _ExecutedStep | None:
        """Return the most recently executed step, for re-display."""
        if not self._history:
            return None
        return self._history[-1]

    def stop(self) -> None:
        """Mark the workflow as aborted. Pending steps become SKIPPED."""
        if self._stopped:
            return
        self._stopped = True
        consumed = len(self._history)
        for skipped in self._engine.workflow.steps[consumed:]:
            placeholder = CommandResult(command=skipped.command, status=ExecutionStatus.SKIPPED)
            placeholder.mark_finished(ExecutionStatus.SKIPPED)
            self._report.results.append(placeholder)
            self._report.skipped += 1

    def peek(self) -> object | None:
        """Return the :class:`Step` that will execute next, or None."""
        idx = self.cursor
        if idx >= len(self._engine.workflow):
            return None
        return self._engine.workflow.steps[idx]
