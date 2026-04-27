"""Workflow execution engine.

The engine is iterator-shaped: callers drive it step-by-step through
:meth:`WorkflowEngine.iter_steps` (used by both the linear runner and the
debugger). For ergonomic batch runs there's :meth:`run_all`, which calls into
the iterator and aggregates results.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from workflow_runner.execution.executor import CommandExecutor, StreamHandler
from workflow_runner.execution.result import CommandResult, ExecutionStatus
from workflow_runner.logging_utils import get_logger
from workflow_runner.workflow.model import OnFailure, Step, Workflow


class StepEventKind(str, Enum):
    STARTED = "started"
    FINISHED = "finished"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass
class StepEvent:
    """Emitted for every transition during workflow execution."""

    kind: StepEventKind
    index: int
    step: Step
    result: CommandResult | None = None


@dataclass
class WorkflowReport:
    """Aggregated outcome of running a workflow."""

    workflow: str
    total: int
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    aborted: int = 0
    blocked: int = 0
    results: list[CommandResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.aborted == 0 and self.blocked == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow": self.workflow,
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "aborted": self.aborted,
            "blocked": self.blocked,
            "ok": self.ok,
            "results": [r.to_dict() for r in self.results],
        }


PromptCallback = Callable[[Step, CommandResult], bool]
"""Return True to continue after a failed ``on_failure: prompt`` step."""


class WorkflowEngine:
    """Drive a :class:`Workflow` against a :class:`CommandExecutor`."""

    def __init__(
        self,
        workflow: Workflow,
        executor: CommandExecutor,
        *,
        stream: StreamHandler | None = None,
        prompt_on_failure: PromptCallback | None = None,
    ) -> None:
        self._workflow = workflow
        self._executor = executor
        self._stream = stream
        self._prompt_on_failure = prompt_on_failure
        self._log = get_logger("workflow_runner.engine", workflow=workflow.name)

    @property
    def workflow(self) -> Workflow:
        return self._workflow

    def run_all(self) -> WorkflowReport:
        report = WorkflowReport(workflow=self._workflow.name, total=len(self._workflow))
        for event in self.iter_steps():
            if event.kind is StepEventKind.STARTED:
                continue
            if event.result is None:
                continue
            report.results.append(event.result)
            self._tally(report, event.result)
            if not self._should_continue(event.step, event.result):
                # Mark the rest as skipped so the report reflects reality.
                consumed = len(report.results)
                for skipped in self._workflow.steps[consumed:]:
                    placeholder = CommandResult(
                        command=skipped.command, status=ExecutionStatus.SKIPPED
                    )
                    placeholder.mark_finished(ExecutionStatus.SKIPPED)
                    report.results.append(placeholder)
                    report.skipped += 1
                break
        return report

    def iter_steps(self) -> Iterator[StepEvent]:
        """Yield events as the workflow progresses.

        A typical sequence per step is ``STARTED`` then ``FINISHED`` (or
        ``SKIPPED`` / ``BLOCKED``). Consumers that want to interleave control
        (e.g. the debugger) drive this generator directly.
        """
        for index, step in enumerate(self._workflow.steps):
            if step.skip:
                self._log.info("skipping step", extra={"step": step.name})
                placeholder = CommandResult(command=step.command, status=ExecutionStatus.SKIPPED)
                placeholder.mark_finished(ExecutionStatus.SKIPPED)
                yield StepEvent(StepEventKind.SKIPPED, index, step, placeholder)
                continue

            yield StepEvent(StepEventKind.STARTED, index, step, None)

            env = {**self._workflow.default_env, **step.env}
            cwd = step.cwd or self._workflow.default_cwd
            timeout = step.timeout if step.timeout is not None else self._workflow.default_timeout

            self._log.info(
                "executing step",
                extra={"step": step.name, "command": step.command, "cwd": cwd, "timeout": timeout},
            )
            result = self._executor.run(
                step.command,
                env=env or None,
                cwd=cwd,
                timeout=timeout,
                stream=self._stream,
            )
            # Re-evaluate "success" against the step's expected exit codes.
            if result.status is ExecutionStatus.FAILURE and result.exit_code in step.expect_exit_codes:
                result.status = ExecutionStatus.SUCCESS
            elif result.status is ExecutionStatus.SUCCESS and result.exit_code not in step.expect_exit_codes:
                result.status = ExecutionStatus.FAILURE
                result.error = (
                    f"exit code {result.exit_code} not in expected {list(step.expect_exit_codes)}"
                )

            kind = StepEventKind.BLOCKED if result.status is ExecutionStatus.BLOCKED else StepEventKind.FINISHED
            yield StepEvent(kind, index, step, result)

    # ----------------------------------------------------------- internals
    def _should_continue(self, step: Step, result: CommandResult) -> bool:
        if result.succeeded or result.status is ExecutionStatus.SKIPPED:
            return True
        if step.on_failure is OnFailure.CONTINUE:
            self._log.warning(
                "step failed but on_failure=continue",
                extra={"step": step.name, "exit_code": result.exit_code},
            )
            return True
        if step.on_failure is OnFailure.PROMPT and self._prompt_on_failure is not None:
            if self._prompt_on_failure(step, result):
                return True
        self._log.error(
            "halting workflow due to failed step",
            extra={"step": step.name, "exit_code": result.exit_code, "status": result.status.value},
        )
        return False

    @staticmethod
    def _tally(report: WorkflowReport, result: CommandResult) -> None:
        if result.status is ExecutionStatus.SUCCESS:
            report.succeeded += 1
        elif result.status is ExecutionStatus.SKIPPED:
            report.skipped += 1
        elif result.status is ExecutionStatus.BLOCKED:
            report.blocked += 1
        elif result.status is ExecutionStatus.ABORTED:
            report.aborted += 1
        else:
            report.failed += 1
