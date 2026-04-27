"""Tests for the YAML/JSON workflow loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow_runner.workflow.loader import WorkflowLoadError, load_workflow
from workflow_runner.workflow.model import OnFailure


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_loads_basic_yaml(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.yaml",
        """
name: hello
description: greet
steps:
  - name: say
    command: echo hi
""",
    )
    workflow = load_workflow(path)
    assert workflow.name == "hello"
    assert len(workflow) == 1
    assert workflow.steps[0].command == "echo hi"
    assert workflow.steps[0].on_failure is OnFailure.STOP


def test_loads_basic_json(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.json",
        json.dumps(
            {
                "name": "hello",
                "steps": [{"name": "ls", "command": "ls"}],
            }
        ),
    )
    workflow = load_workflow(path)
    assert workflow.name == "hello"


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "wf.yaml", "name: x\nfoo: bar\nsteps:\n  - {name: a, command: b}\n")
    with pytest.raises(WorkflowLoadError):
        load_workflow(path)


def test_unknown_step_key_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.yaml",
        "name: x\nsteps:\n  - {name: a, command: b, oops: 1}\n",
    )
    with pytest.raises(WorkflowLoadError):
        load_workflow(path)


def test_duplicate_step_names_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.yaml",
        "name: x\nsteps:\n  - {name: a, command: b}\n  - {name: a, command: c}\n",
    )
    with pytest.raises(WorkflowLoadError):
        load_workflow(path)


def test_invalid_on_failure_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.yaml",
        "name: x\nsteps:\n  - {name: a, command: b, on_failure: maybe}\n",
    )
    with pytest.raises(WorkflowLoadError):
        load_workflow(path)


def test_expect_exit_codes_must_be_ints(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.yaml",
        "name: x\nsteps:\n  - {name: a, command: b, expect_exit_codes: [\"0\"]}\n",
    )
    with pytest.raises(WorkflowLoadError):
        load_workflow(path)


def test_env_must_be_mapping(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.yaml",
        "name: x\nsteps:\n  - {name: a, command: b, env: not-a-mapping}\n",
    )
    with pytest.raises(WorkflowLoadError):
        load_workflow(path)


def test_loads_full_step(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.yaml",
        """
name: full
default_timeout: 60
steps:
  - name: a
    command: echo hi
    description: greet
    cwd: /tmp
    env: {FOO: bar}
    timeout: 5
    on_failure: continue
    confirm: true
    skip: false
    tags: [smoke]
    expect_exit_codes: [0, 2]
""",
    )
    wf = load_workflow(path)
    step = wf.steps[0]
    assert step.cwd == "/tmp"
    assert step.env == {"FOO": "bar"}
    assert step.timeout == 5.0
    assert step.on_failure is OnFailure.CONTINUE
    assert step.confirm is True
    assert step.tags == ("smoke",)
    assert step.expect_exit_codes == (0, 2)
    assert wf.default_timeout == 60.0
