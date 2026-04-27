"""Tests for the session manager."""

from unittest.mock import MagicMock

import pytest

from workflow_runner.connection.manager import SessionManager
from workflow_runner.connection.session import SessionConfig, SessionState


def _config(host: str = "localhost") -> SessionConfig:
    return SessionConfig(host=host, username="testuser")


class TestSessionManager:
    def test_create_and_get(self):
        mgr = SessionManager()
        sess = mgr.create("s1", _config())
        assert mgr.get("s1") is sess

    def test_create_duplicate_raises(self):
        mgr = SessionManager()
        mgr.create("s1", _config())
        with pytest.raises(ValueError, match="already exists"):
            mgr.create("s1", _config())

    def test_get_unknown_returns_none(self):
        mgr = SessionManager()
        assert mgr.get("ghost") is None

    def test_remove_calls_disconnect(self):
        mgr = SessionManager()
        sess = mgr.create("s1", _config())
        sess.disconnect = MagicMock()
        mgr.remove("s1")
        assert mgr.get("s1") is None
        sess.disconnect.assert_called_once()

    def test_remove_unknown_is_noop(self):
        mgr = SessionManager()
        mgr.remove("nonexistent")  # should not raise

    def test_list_ids(self):
        mgr = SessionManager()
        mgr.create("a", _config("host-a"))
        mgr.create("b", _config("host-b"))
        ids = mgr.list_ids()
        assert "a" in ids
        assert "b" in ids

    def test_sessions_snapshot(self):
        mgr = SessionManager()
        mgr.create("x", _config())
        snapshot = mgr.sessions
        # Mutating the snapshot must not affect the manager
        snapshot.pop("x")
        assert mgr.get("x") is not None

    def test_disconnect_all(self):
        mgr = SessionManager()
        s1 = mgr.create("s1", _config("h1"))
        s2 = mgr.create("s2", _config("h2"))
        s1.disconnect = MagicMock()
        s2.disconnect = MagicMock()
        mgr.disconnect_all()
        s1.disconnect.assert_called_once()
        s2.disconnect.assert_called_once()
        assert mgr.list_ids() == []


class TestSessionConfig:
    def test_default_username_from_os(self):
        import getpass
        config = SessionConfig(host="myhost")
        assert config.username == getpass.getuser()

    def test_explicit_username(self):
        config = SessionConfig(host="myhost", username="alice")
        assert config.username == "alice"

    def test_label(self):
        from workflow_runner.connection.session import Session
        sess = Session("test", SessionConfig(host="10.0.0.1", port=2222, username="bob"))
        assert sess.label == "bob@10.0.0.1:2222"


class TestWorkflowEngine:
    """Unit tests for WorkflowEngine using a mocked session."""

    def _make_engine(self):
        from workflow_runner.connection.session import Session
        from workflow_runner.workflow.engine import WorkflowEngine

        mock_sess = MagicMock(spec=Session)
        mock_sess.session_id = "mock"
        engine = WorkflowEngine(mock_sess)
        return engine, mock_sess

    def _make_result(self, exit_code: int = 0):
        from datetime import datetime
        from workflow_runner.executor.result import CommandResult
        return CommandResult(
            command="echo",
            exit_code=exit_code,
            stdout="output\n",
            stderr="",
            execution_time=0.01,
            timestamp=datetime.now(),
            session_id="mock",
        )

    def test_run_all_steps_success(self):
        from workflow_runner.workflow.models import Workflow, Step
        from workflow_runner.workflow.engine import WorkflowStatus

        engine, mock_sess = self._make_engine()
        mock_sess.execute.return_value = self._make_result(0)

        wf = Workflow(name="t", description="", steps=[
            Step(name="s1", command="echo a"),
            Step(name="s2", command="echo b"),
        ])
        run = engine.run(wf)
        assert run.status == WorkflowStatus.COMPLETED
        assert len(run.step_results) == 2
        assert mock_sess.execute.call_count == 2

    def test_run_stops_on_failure(self):
        from workflow_runner.workflow.models import Workflow, Step
        from workflow_runner.workflow.engine import WorkflowStatus

        engine, mock_sess = self._make_engine()
        mock_sess.execute.return_value = self._make_result(1)

        wf = Workflow(name="t", description="", steps=[
            Step(name="fail", command="false"),
            Step(name="never", command="echo should_not_run"),
        ])
        run = engine.run(wf)
        assert run.status == WorkflowStatus.FAILED
        # Second step should never execute
        assert mock_sess.execute.call_count == 1

    def test_allow_failure_continues(self):
        from workflow_runner.workflow.models import Workflow, Step
        from workflow_runner.workflow.engine import WorkflowStatus

        engine, mock_sess = self._make_engine()
        # First step fails (allow_failure=True), second succeeds
        mock_sess.execute.side_effect = [
            self._make_result(1),
            self._make_result(0),
        ]

        wf = Workflow(name="t", description="", steps=[
            Step(name="soft_fail", command="false", allow_failure=True),
            Step(name="next", command="echo ok"),
        ])
        run = engine.run(wf)
        assert run.status == WorkflowStatus.COMPLETED
        assert mock_sess.execute.call_count == 2

    def test_debugger_step_next(self):
        from workflow_runner.workflow.models import Workflow, Step
        from workflow_runner.workflow.engine import DebuggerState

        engine, mock_sess = self._make_engine()
        mock_sess.execute.return_value = self._make_result(0)

        wf = Workflow(name="t", description="", steps=[
            Step(name="s1", command="pwd"),
            Step(name="s2", command="id"),
        ])
        dbg = engine.create_debugger(wf)
        dbg.start()

        assert dbg.state == DebuggerState.PAUSED
        assert dbg.current_index == 0

        sr1 = dbg.step_next()
        assert sr1 is not None
        assert sr1.success
        assert dbg.current_index == 1

        sr2 = dbg.step_next()
        assert sr2 is not None
        assert dbg.is_done
        assert dbg.state == DebuggerState.COMPLETED

    def test_debugger_abort(self):
        from workflow_runner.workflow.models import Workflow, Step
        from workflow_runner.workflow.engine import DebuggerState, WorkflowStatus

        engine, mock_sess = self._make_engine()
        wf = Workflow(name="t", description="", steps=[Step(name="s", command="ls")])
        dbg = engine.create_debugger(wf)
        dbg.start()
        dbg.step_abort()
        assert dbg.state == DebuggerState.ABORTED
        assert dbg.run.status == WorkflowStatus.ABORTED

    def test_debugger_get_previous_result(self):
        from workflow_runner.workflow.models import Workflow, Step

        engine, mock_sess = self._make_engine()
        mock_sess.execute.return_value = self._make_result(0)

        wf = Workflow(name="t", description="", steps=[
            Step(name="first", command="echo first"),
        ])
        dbg = engine.create_debugger(wf)
        dbg.start()
        dbg.step_next()
        # After stepping, index is 1; previous result is at index 0
        assert dbg.get_step_result(0) is not None
        assert dbg.get_step_result(99) is None
