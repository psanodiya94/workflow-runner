"""Load workflows from YAML, JSON, or Python module files."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

from workflow_runner.workflow.models import Workflow


def load_workflow(path: str | Path) -> Workflow:
    """
    Parse a workflow definition file and return a :class:`Workflow`.

    Supported formats:
      * ``.yaml`` / ``.yml`` — YAML document
      * ``.json``            — JSON object
      * ``.py``              — Python module exposing a top-level ``WORKFLOW`` dict

    Raises :class:`FileNotFoundError` if the file does not exist.
    Raises :class:`ValueError` for unsupported file extensions.
    Raises :class:`AttributeError` for Python modules missing ``WORKFLOW``.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Workflow file not found: {p}")

    suffix = p.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        data = _load_yaml(p)
    elif suffix == ".json":
        data = _load_json(p)
    elif suffix == ".py":
        data = _load_python(p)
    else:
        raise ValueError(
            f"Unsupported workflow format '{suffix}'. "
            "Use .yaml, .yml, .json, or .py"
        )

    return Workflow.from_dict(data)


# ------------------------------------------------------------------
# Format-specific parsers
# ------------------------------------------------------------------

def _load_yaml(p: Path) -> dict:
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_json(p: Path) -> dict:
    with p.open(encoding="utf-8") as fh:
        return json.load(fh)


def _load_python(p: Path) -> dict:
    spec = importlib.util.spec_from_file_location("_wfr_workflow_module", p)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load Python module from {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    if not hasattr(mod, "WORKFLOW"):
        raise AttributeError(
            f"{p} must define a top-level 'WORKFLOW' dictionary"
        )
    return mod.WORKFLOW  # type: ignore[no-any-return]
