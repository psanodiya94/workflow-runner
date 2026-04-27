"""Structured logging with per-session and per-workflow file sinks."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_LOG_DIR = Path.home() / ".workflow_runner" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_FMT = logging.Formatter(
    fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)


def get_logger(
    name: str,
    *,
    session_id: Optional[str] = None,
    log_file: Optional[Path] = None,
) -> logging.Logger:
    """Return (or create) a named logger with console + rotating file handlers."""
    logger = logging.getLogger(f"wfr.{name}")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # Console: WARNING and above (toggled by set_verbosity)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(_FMT)
    logger.addHandler(console_handler)

    # File: all levels
    if log_file is None:
        ts = datetime.now().strftime("%Y%m%d")
        suffix = f"_{session_id}" if session_id else ""
        log_file = _LOG_DIR / f"{name}{suffix}_{ts}.log"

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_FMT)
    logger.addHandler(file_handler)

    return logger


def set_verbosity(verbose: bool) -> None:
    """Switch console handlers to DEBUG (verbose=True) or WARNING (verbose=False)."""
    level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger("wfr")
    for name, lgr in logging.Logger.manager.loggerDict.items():
        if not name.startswith("wfr"):
            continue
        if not isinstance(lgr, logging.Logger):
            continue
        for handler in lgr.handlers:
            if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stderr:
                handler.setLevel(level)
