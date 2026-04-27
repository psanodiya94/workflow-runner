"""Workflow execution engine and GDB-like step debugger."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional

from workflow_runner.executor.command import build_env_prefix, is_destructive
from workflow_runner.logger import get_logger
from workflow_runner.workflow.models import Step, Workflow

if TYPE_CHECKING:
    from workflow_runner.connection.session import Session
    from workflow_runner.executor.result import CommandResult


# ──────────────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────────────

class WorkflowStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class StepResult:
    """Outcome of executing a single workflow step."""

    step: Step
    result: Optional["CommandResult"] = None
    skipped: bool = False   # user declined to execute (e.g. destructive confirm)
    error: Optional[str] = None  # exception message if execution raised

    @property
    def success(self) -> bool:
        if self.skipped:
            return True
        if self.error:
            return self.step.allow_failure
        if self.result is None:
            return False
        return self.result.success or self.step.allow_failure


@dataclass
class WorkflowRun:
    """Accumulated state for one execution of a :class:`Workflow`."""

    workflow: Workflow
    session_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    step_results: list[StepResult] = field(default_factory=list)
    current_step: int = 0
    start_time: Optional[float] = None
    end_time: Optional[float] = None

    @property
    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        return (self.end_time or time.monotonic()) - self.start_time

    @property
    def total_steps(self) -> int:
        return len(self.workflow.steps)


# ──────────────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────────────

class WorkflowEngine:
    """
    Executes workflows against a connected :class:`Session`.

    Two execution modes:

    * :meth:`run` — execute all steps sequentially, stopping on failure.
    * :meth:`create_debugger` — return a :class:`WorkflowDebugger` for
      interactive step-by-step execution.
    """

    def __init__(self, session: "Session") -> None:
        self._session = session
        self._log = get_logger(
            "workflow.engine", session_id=session.session_id
        )

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------

    def run(
        self,
        workflow: Workflow,
        *,
        on_step_start: Optional[Callable[[int, Step], None]] = None,
        on_step_done: Optional[Callable[[int, StepResult], None]] = None,
        on_confirm: Optional[Callable[[Step], bool]] = None,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
    ) -> WorkflowRun:
        """
        Execute all steps in order.

        Callbacks:
          ``on_step_start(index, step)``   — called before each step.
          ``on_step_done(index, result)``  — called after each step.
          ``on_confirm(step) -> bool``     — called for destructive steps;
                                             return False to skip.
          ``on_stdout / on_stderr``        — called with streamed chunks.
        """
        run = WorkflowRun(workflow=workflow, session_id=self._session.session_id)
        run.status = WorkflowStatus.RUNNING
        run.start_time = time.monotonic()
        merged_env = dict(workflow.environment)

        for i, step in enumerate(workflow.steps):
            run.current_step = i

            if on_step_start:
                on_step_start(i, step)

            # Require confirmation for flagged or destructive commands
            if (step.confirm_before or is_destructive(step.command)) and on_confirm:
                if not on_confirm(step):
                    sr = StepResult(step=step, skipped=True)
                    run.step_results.append(sr)
                    if on_step_done:
                        on_step_done(i, sr)
                    continue

            sr = self._execute_step(
                step, merged_env, on_stdout=on_stdout, on_stderr=on_stderr
            )
            run.step_results.append(sr)

            if on_step_done:
                on_step_done(i, sr)

            if not sr.success:
                run.status = WorkflowStatus.FAILED
                run.end_time = time.monotonic()
                return run

        run.status = WorkflowStatus.COMPLETED
        run.end_time = time.monotonic()
        return run

    # ------------------------------------------------------------------
    # Debugger factory
    # ------------------------------------------------------------------

    def create_debugger(self, workflow: Workflow) -> "WorkflowDebugger":
        """Return a :class:`WorkflowDebugger` bound to this engine."""
        return WorkflowDebugger(workflow=workflow, engine=self)

    # ------------------------------------------------------------------
    # Internal step executor (shared by engine and debugger)
    # ------------------------------------------------------------------

    def _execute_step(
        self,
        step: Step,
        base_env: dict[str, str],
        *,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
    ) -> StepResult:
        env = {**base_env, **step.environment}
        # Inject env vars as a portable shell prefix instead of relying on
        # sshd AcceptEnv, which is often disabled.
        effective_command = build_env_prefix(env) + step.command

        self._log.info("Executing step '%s': %s", step.name, step.command)
        try:
            result = self._session.execute(
                effective_command,
                timeout=step.timeout,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
            return StepResult(step=step, result=result)
        except Exception as exc:
            self._log.error("Step '%s' raised: %s", step.name, exc)
            return StepResult(step=step, error=str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# GDB-like step debugger
# ──────────────────────────────────────────────────────────────────────────────

class DebuggerState(Enum):
    READY = "ready"
    PAUSED = "paused"       # waiting for the user's next command
    COMPLETED = "completed"
    ABORTED = "aborted"


class WorkflowDebugger:
    """
    Step-by-step workflow execution modelled after gdb's ``next`` / ``continue``.

    Typical lifecycle::

        dbg = engine.create_debugger(workflow)
        dbg.start()                       # enters PAUSED state

        while not dbg.is_done:
            sr = dbg.step_next()          # execute one step, stay PAUSED
            # … inspect sr …

        # or: dbg.step_continue()         # run all remaining steps
        # or: dbg.step_abort()            # abort immediately

    Attach callbacks before calling :meth:`start`:

        dbg.on_confirm = lambda step: input("Execute? [y/N] ").lower() == "y"
    """

    def __init__(self, workflow: Workflow, engine: WorkflowEngine) -> None:
        self._workflow = workflow
        self._engine = engine
        self._run = WorkflowRun(
            workflow=workflow, session_id=engine._session.session_id
        )
        self._state = DebuggerState.READY
        self._current = 0
        self._merged_env: dict[str, str] = dict(workflow.environment)

        # Optional hooks set by the caller
        self.on_confirm: Optional[Callable[[Step], bool]] = None
        self.on_step_output: Optional[Callable[[str, str], None]] = None

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------

    @property
    def state(self) -> DebuggerState:
        return self._state

    @property
    def current_index(self) -> int:
        return self._current

    @property
    def total_steps(self) -> int:
        return len(self._workflow.steps)

    @property
    def current_step(self) -> Optional[Step]:
        if self._current < self.total_steps:
            return self._workflow.steps[self._current]
        return None

    @property
    def run(self) -> WorkflowRun:
        return self._run

    @property
    def is_done(self) -> bool:
        return self._state in (DebuggerState.COMPLETED, DebuggerState.ABORTED)

    def get_step_result(self, index: int) -> Optional[StepResult]:
        """Return the result of an already-executed step by index, or None."""
        if 0 <= index < len(self._run.step_results):
            return self._run.step_results[index]
        return None

    # ------------------------------------------------------------------
    # Debugger controls
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Initialise the run and enter PAUSED state, ready for the first step."""
        if self._state != DebuggerState.READY:
            return
        self._run.start_time = time.monotonic()
        self._run.status = WorkflowStatus.RUNNING
        self._state = DebuggerState.PAUSED

    def step_next(self) -> Optional[StepResult]:
        """
        Execute the current step and advance the cursor.

        Returns the :class:`StepResult` or None if already finished.
        Remains in PAUSED state unless the workflow is exhausted or a
        non-recoverable step failure occurs.
        """
        if self._state != DebuggerState.PAUSED:
            return None
        if self._current >= self.total_steps:
            self._finish()
            return None

        step = self._workflow.steps[self._current]

        # Confirm destructive step via hook
        if (step.confirm_before or is_destructive(step.command)) and self.on_confirm:
            if not self.on_confirm(step):
                sr = StepResult(step=step, skipped=True)
                self._run.step_results.append(sr)
                self._current += 1
                self._check_terminal()
                return sr

        def _stdout(chunk: str) -> None:
            if self.on_step_output:
                self.on_step_output(chunk, "")

        def _stderr(chunk: str) -> None:
            if self.on_step_output:
                self.on_step_output("", chunk)

        sr = self._engine._execute_step(
            step,
            self._merged_env,
            on_stdout=_stdout,
            on_stderr=_stderr,
        )
        self._run.step_results.append(sr)
        self._current += 1
        self._check_terminal()
        return sr

    def step_continue(self) -> WorkflowRun:
        """Execute all remaining steps without pausing."""
        while self._state == DebuggerState.PAUSED and self._current < self.total_steps:
            sr = self.step_next()
            if sr and not sr.success:
                break
        return self._run

    def step_abort(self) -> WorkflowRun:
        """Abort execution immediately and mark the run as ABORTED."""
        self._state = DebuggerState.ABORTED
        self._run.status = WorkflowStatus.ABORTED
        self._run.end_time = time.monotonic()
        return self._run

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_terminal(self) -> None:
        """Transition to a terminal state when appropriate."""
        if self._current >= self.total_steps:
            self._finish()
            return
        # A failed step (not allowed to fail) aborts the run
        if self._run.step_results:
            last = self._run.step_results[-1]
            if not last.success:
                self._run.status = WorkflowStatus.FAILED
                self._state = DebuggerState.ABORTED
                self._run.end_time = time.monotonic()

    def _finish(self) -> None:
        self._state = DebuggerState.COMPLETED
        self._run.status = WorkflowStatus.COMPLETED
        self._run.end_time = time.monotonic()
