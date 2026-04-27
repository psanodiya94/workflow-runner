"""workflow-runner: persistent remote command execution and workflow runner."""

from importlib import metadata

try:
    __version__ = metadata.version("workflow-runner")
except metadata.PackageNotFoundError:  # pragma: no cover - source checkout
    __version__ = "0.1.0"

__all__ = ["__version__"]
