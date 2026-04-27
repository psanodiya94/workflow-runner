"""Tests for the workflow engine and step-by-step debugger."""

from __future__ import annotations

from workflow_runner.debugger.stepper import WorkflowDebugger
from workflow_runner.execution.executor import CommandExecutor
from workflow_runner.execution.result import ExecutionStatus
from workflow_runner.workflow.engine import WorkflowEngine
from workflow_runner.workflow.model import OnFailure, Step, Workflow


def _wf(steps: list[Step]) -> Workflow:
    return Workflow(name="t", steps=tuple(steps))


def test_engine_runs_all_success(fake_connection_factory) -> None:
    conn = fake_connection_factory(("a\n", "", 0), ("b\n", "", 0))
    executor = CommandExecutor(conn)
    workflow = _wf([Step("first", "echo a"), Step("second", "echo b")])
    report = WorkflowEngine(workflow, executor).run_all()
    assert report.ok
    assert report.succeeded == 2
    assert report.failed == 0


def test_engine_stops_on_failure_by_default(fake_connection_factory) -> None:
    conn = fake_connection_factory(("", "", 0), ("", "boom", 1), ("never", "", 0))
    executor = CommandExecutor(conn)
    workflow = _wf([Step("a", "echo"), Step("b", "false"), Step("c", "echo c")])
    report = WorkflowEngine(workflow, executor).run_all()
    assert not report.ok
    assert report.succeeded == 1
    assert report.failed == 1
    assert report.skipped == 1
    # third command should never have been sent.
    assert conn.commands == ["echo", "false"]


def test_engine_continues_when_step_says_continue(fake_connection_factory) -> None:
    conn = fake_connection_factory(("", "", 0), ("", "boom", 1), ("c", "", 0))
    executor = CommandExecutor(conn)
    workflow = _wf(
        [
            Step("a", "echo"),
            Step("b", "false", on_failure=OnFailure.CONTINUE),
            Step("c", "echo c"),
        ]
    )
    report = WorkflowEngine(workflow, executor).run_all()
    assert report.failed == 1
    assert report.succeeded == 2


def test_expect_exit_codes_treats_nonzero_as_success(fake_connection_factory) -> None:
    conn = fake_connection_factory(("", "", 2))
    executor = CommandExecutor(conn)
    workflow = _wf([Step("a", "grep x", expect_exit_codes=(0, 2))])
    report = WorkflowEngine(workflow, executor).run_all()
    assert report.ok
    assert report.succeeded == 1


def test_skip_step_marked_skipped(fake_connection_factory) -> None:
    conn = fake_connection_factory(("ran\n", "", 0))
    executor = CommandExecutor(conn)
    workflow = _wf([Step("skipped", "echo skipped", skip=True), Step("ran", "echo ran")])
    report = WorkflowEngine(workflow, executor).run_all()
    assert report.skipped == 1
    assert report.succeeded == 1
    assert conn.commands == ["echo ran"]


def test_debugger_step_then_continue(fake_connection_factory) -> None:
    conn = fake_connection_factory(("a\n", "", 0), ("b\n", "", 0), ("c\n", "", 0))
    executor = CommandExecutor(conn)
    workflow = _wf(
        [Step("a", "echo a"), Step("b", "echo b"), Step("c", "echo c")]
    )
    debugger = WorkflowDebugger(WorkflowEngine(workflow, executor))

    first = debugger.step()
    assert first is not None
    assert first.name == "a"
    assert first.result.status is ExecutionStatus.SUCCESS

    report = debugger.continue_remaining()
    assert report.succeeded == 3
    assert debugger.is_done


def test_debugger_stop_marks_remaining_skipped(fake_connection_factory) -> None:
    conn = fake_connection_factory(("a\n", "", 0))
    executor = CommandExecutor(conn)
    workflow = _wf([Step("a", "echo a"), Step("b", "echo b"), Step("c", "echo c")])
    debugger = WorkflowDebugger(WorkflowEngine(workflow, executor))
    debugger.step()
    debugger.stop()
    assert debugger.is_done
    assert debugger.report.succeeded == 1
    assert debugger.report.skipped == 2


def test_debugger_previous_returns_last_step(fake_connection_factory) -> None:
    conn = fake_connection_factory(("a\n", "", 0))
    executor = CommandExecutor(conn)
    workflow = _wf([Step("a", "echo a")])
    debugger = WorkflowDebugger(WorkflowEngine(workflow, executor))
    assert debugger.previous() is None
    debugger.step()
    last = debugger.previous()
    assert last is not None
    assert last.name == "a"
