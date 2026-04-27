"""Load workflow definitions from YAML or JSON files.

Schema (top-level keys):

    name: str                  # required
    description: str
    version: str               # default "1"
    default_cwd: str
    default_env: { KEY: VAL }
    default_timeout: float
    tags: [str, ...]
    steps:
      - name: str              # required
        command: str           # required
        description: str
        cwd: str
        env: { KEY: VAL }
        timeout: float
        on_failure: stop | continue | prompt
        confirm: bool
        skip: bool
        tags: [str, ...]
        expect_exit_codes: [int, ...]   # default [0]

Unknown keys are rejected so typos don't silently change behavior.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from workflow_runner.workflow.model import OnFailure, Step, Workflow

_TOP_LEVEL_KEYS = {
    "name",
    "description",
    "version",
    "default_cwd",
    "default_env",
    "default_timeout",
    "tags",
    "steps",
}
_STEP_KEYS = {
    "name",
    "command",
    "description",
    "cwd",
    "env",
    "timeout",
    "on_failure",
    "confirm",
    "skip",
    "tags",
    "expect_exit_codes",
}


class WorkflowLoadError(ValueError):
    """Raised for any problem parsing or validating a workflow file."""


def load_workflow(path: str | Path) -> Workflow:
    """Parse the file at ``path`` and return a :class:`Workflow`.

    The format is inferred from the file extension. Suffixes ``.yaml`` /
    ``.yml`` use YAML; everything else is parsed as JSON.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise WorkflowLoadError(f"workflow file not found: {p}")
    text = p.read_text(encoding="utf-8")
    try:
        if p.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise WorkflowLoadError(f"could not parse {p}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise WorkflowLoadError(f"{p}: top-level value must be a mapping")
    return _build_workflow(data, source=str(p))


def _build_workflow(data: Mapping[str, Any], *, source: str) -> Workflow:
    unknown = set(data.keys()) - _TOP_LEVEL_KEYS
    if unknown:
        raise WorkflowLoadError(f"{source}: unknown top-level keys: {sorted(unknown)}")
    if "name" not in data:
        raise WorkflowLoadError(f"{source}: missing required key 'name'")
    if "steps" not in data or not isinstance(data["steps"], list):
        raise WorkflowLoadError(f"{source}: 'steps' must be a non-empty list")

    steps = tuple(_build_step(step, source=source, index=i) for i, step in enumerate(data["steps"]))

    try:
        return Workflow(
            name=str(data["name"]),
            steps=steps,
            description=str(data.get("description", "")),
            version=str(data.get("version", "1")),
            default_cwd=_optional_str(data.get("default_cwd"), "default_cwd", source),
            default_env=_validate_env(data.get("default_env"), "default_env", source),
            default_timeout=_optional_float(data.get("default_timeout"), "default_timeout", source),
            tags=tuple(str(t) for t in data.get("tags", ()) or ()),
        )
    except ValueError as exc:
        raise WorkflowLoadError(f"{source}: {exc}") from exc


def _build_step(raw: Any, *, source: str, index: int) -> Step:
    if not isinstance(raw, Mapping):
        raise WorkflowLoadError(f"{source}: step #{index} must be a mapping")
    unknown = set(raw.keys()) - _STEP_KEYS
    if unknown:
        raise WorkflowLoadError(f"{source}: step #{index}: unknown keys {sorted(unknown)}")
    if "name" not in raw or "command" not in raw:
        raise WorkflowLoadError(
            f"{source}: step #{index}: 'name' and 'command' are required"
        )
    on_failure_raw = str(raw.get("on_failure", "stop")).lower()
    try:
        on_failure = OnFailure(on_failure_raw)
    except ValueError:
        raise WorkflowLoadError(
            f"{source}: step {raw['name']!r}: invalid on_failure {on_failure_raw!r}"
        ) from None

    expect_codes_raw = raw.get("expect_exit_codes", [0])
    if not isinstance(expect_codes_raw, list) or not all(isinstance(c, int) for c in expect_codes_raw):
        raise WorkflowLoadError(
            f"{source}: step {raw['name']!r}: expect_exit_codes must be a list of ints"
        )

    try:
        return Step(
            name=str(raw["name"]),
            command=str(raw["command"]),
            description=str(raw.get("description", "")),
            cwd=_optional_str(raw.get("cwd"), "cwd", f"step {raw['name']}"),
            env=_validate_env(raw.get("env"), "env", f"step {raw['name']}"),
            timeout=_optional_float(raw.get("timeout"), "timeout", f"step {raw['name']}"),
            on_failure=on_failure,
            confirm=bool(raw.get("confirm", False)),
            skip=bool(raw.get("skip", False)),
            tags=tuple(str(t) for t in raw.get("tags", ()) or ()),
            expect_exit_codes=tuple(expect_codes_raw),
        )
    except ValueError as exc:
        raise WorkflowLoadError(f"{source}: {exc}") from exc


def _optional_str(value: Any, field: str, ctx: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowLoadError(f"{ctx}: '{field}' must be a string")
    return value


def _optional_float(value: Any, field: str, ctx: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise WorkflowLoadError(f"{ctx}: '{field}' must be a number")
    return float(value)


def _validate_env(value: Any, field: str, ctx: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WorkflowLoadError(f"{ctx}: '{field}' must be a mapping of str -> str")
    cleaned: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str):
            raise WorkflowLoadError(f"{ctx}: '{field}' keys must be strings")
        cleaned[key] = str(val)
    return cleaned
