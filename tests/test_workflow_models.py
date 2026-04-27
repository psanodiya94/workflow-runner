"""Tests for workflow data models."""

from datetime import datetime

import pytest

from workflow_runner.executor.result import CommandResult
from workflow_runner.workflow.models import Step, Workflow


class TestStep:
    def test_defaults(self):
        step = Step(name="s", command="echo hi")
        assert step.description == ""
        assert step.timeout is None
        assert step.allow_failure is False
        assert step.confirm_before is False
        assert step.environment == {}

    def test_custom_values(self):
        step = Step(
            name="deploy",
            command="make deploy",
            description="Deploy the app",
            timeout=60.0,
            allow_failure=True,
            confirm_before=True,
            environment={"ENV": "prod"},
        )
        assert step.timeout == 60.0
        assert step.allow_failure is True
        assert step.confirm_before is True
        assert step.environment["ENV"] == "prod"


class TestWorkflow:
    def _simple_data(self) -> dict:
        return {
            "name": "test_wf",
            "description": "A test workflow",
            "version": "2.0",
            "steps": [
                {"name": "s1", "command": "echo hello"},
                {"name": "s2", "command": "ls /tmp", "allow_failure": True},
            ],
        }

    def test_from_dict_basic(self):
        wf = Workflow.from_dict(self._simple_data())
        assert wf.name == "test_wf"
        assert wf.description == "A test workflow"
        assert wf.version == "2.0"
        assert len(wf.steps) == 2

    def test_step_names_and_commands(self):
        wf = Workflow.from_dict(self._simple_data())
        assert wf.steps[0].name == "s1"
        assert wf.steps[0].command == "echo hello"
        assert wf.steps[1].allow_failure is True

    def test_auto_step_names(self):
        wf = Workflow.from_dict({
            "name": "x",
            "steps": [{"command": "pwd"}, {"command": "id"}],
        })
        assert wf.steps[0].name == "step_1"
        assert wf.steps[1].name == "step_2"

    def test_empty_steps(self):
        wf = Workflow.from_dict({"name": "empty"})
        assert wf.steps == []

    def test_environment_propagation(self):
        wf = Workflow.from_dict({
            "name": "env_test",
            "environment": {"APP": "myapp"},
            "steps": [{"command": "echo $APP", "environment": {"EXTRA": "val"}}],
        })
        assert wf.environment["APP"] == "myapp"
        assert wf.steps[0].environment["EXTRA"] == "val"

    def test_missing_name_defaults(self):
        wf = Workflow.from_dict({"steps": [{"command": "ls"}]})
        assert wf.name == "unnamed"
        assert wf.description == ""
        assert wf.version == "1.0"


class TestCommandResult:
    def _make(self, exit_code: int = 0, stdout: str = "out\n", stderr: str = "") -> CommandResult:
        return CommandResult(
            command="echo out",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            execution_time=0.05,
            timestamp=datetime.now(),
            session_id="test",
        )

    def test_success_true_on_zero(self):
        assert self._make(exit_code=0).success is True

    def test_success_false_on_nonzero(self):
        assert self._make(exit_code=1).success is False

    def test_output_without_stderr(self):
        r = self._make(stdout="hello\n", stderr="")
        assert r.output == "hello\n"

    def test_output_with_stderr(self):
        r = self._make(stdout="out\n", stderr="err\n")
        assert r.output == "out\nerr\n"
