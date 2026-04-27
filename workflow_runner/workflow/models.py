"""Workflow and Step data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Step:
    """A single command within a workflow."""

    name: str
    command: str
    description: str = ""
    timeout: Optional[float] = None
    # When True a non-zero exit code does not abort the workflow
    allow_failure: bool = False
    # Require explicit user confirmation before executing (also auto-set for destructive commands)
    confirm_before: bool = False
    environment: dict[str, str] = field(default_factory=dict)


@dataclass
class Workflow:
    """An ordered collection of steps with shared metadata."""

    name: str
    description: str
    steps: list[Step]
    version: str = "1.0"
    # Base environment merged with per-step environment; step values win on conflict
    environment: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Workflow":
        """Construct a :class:`Workflow` from a plain dictionary (parsed YAML/JSON)."""
        raw_steps = data.get("steps", [])
        steps = [
            Step(
                name=s.get("name", f"step_{i + 1}"),
                command=s["command"],
                description=s.get("description", ""),
                timeout=s.get("timeout"),
                allow_failure=bool(s.get("allow_failure", False)),
                confirm_before=bool(s.get("confirm_before", False)),
                environment=dict(s.get("environment", {})),
            )
            for i, s in enumerate(raw_steps)
        ]
        return cls(
            name=data.get("name", "unnamed"),
            description=data.get("description", ""),
            steps=steps,
            version=str(data.get("version", "1.0")),
            environment=dict(data.get("environment", {})),
        )
