"""Tests for the workflow loader (YAML, JSON, Python formats)."""

import json
from pathlib import Path

import pytest
import yaml

from workflow_runner.workflow.loader import load_workflow
from workflow_runner.workflow.models import Workflow

_SIMPLE: dict = {
    "name": "loader_test",
    "description": "Loader test fixture",
    "steps": [
        {"name": "s1", "command": "echo hello"},
        {"name": "s2", "command": "pwd"},
    ],
}


# ──────────────────────────────────────────────────────────────────────────────
# YAML
# ──────────────────────────────────────────────────────────────────────────────

def test_load_yaml(tmp_path: Path):
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.dump(_SIMPLE))
    wf = load_workflow(p)
    assert isinstance(wf, Workflow)
    assert wf.name == "loader_test"
    assert len(wf.steps) == 2


def test_load_yml_extension(tmp_path: Path):
    p = tmp_path / "wf.yml"
    p.write_text(yaml.dump(_SIMPLE))
    wf = load_workflow(p)
    assert wf.name == "loader_test"


# ──────────────────────────────────────────────────────────────────────────────
# JSON
# ──────────────────────────────────────────────────────────────────────────────

def test_load_json(tmp_path: Path):
    p = tmp_path / "wf.json"
    p.write_text(json.dumps(_SIMPLE))
    wf = load_workflow(p)
    assert wf.name == "loader_test"
    assert wf.steps[1].command == "pwd"


# ──────────────────────────────────────────────────────────────────────────────
# Python
# ──────────────────────────────────────────────────────────────────────────────

def test_load_python(tmp_path: Path):
    p = tmp_path / "wf.py"
    p.write_text(
        "WORKFLOW = {"
        "'name': 'py_wf', "
        "'description': 'python format', "
        "'steps': [{'name': 'p1', 'command': 'echo py'}]"
        "}\n"
    )
    wf = load_workflow(p)
    assert wf.name == "py_wf"
    assert wf.steps[0].command == "echo py"


def test_load_python_missing_workflow_attr(tmp_path: Path):
    p = tmp_path / "bad.py"
    p.write_text("SOMETHING_ELSE = {}\n")
    with pytest.raises(AttributeError, match="WORKFLOW"):
        load_workflow(p)


# ──────────────────────────────────────────────────────────────────────────────
# Error cases
# ──────────────────────────────────────────────────────────────────────────────

def test_load_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_workflow("/nonexistent/path/wf.yaml")


def test_load_unsupported_format(tmp_path: Path):
    p = tmp_path / "wf.toml"
    p.write_text("[workflow]\nname = 'x'\n")
    with pytest.raises(ValueError, match="Unsupported"):
        load_workflow(p)


def test_load_accepts_string_path(tmp_path: Path):
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.dump(_SIMPLE))
    wf = load_workflow(str(p))  # string, not Path
    assert wf.name == "loader_test"
