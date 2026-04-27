"""Tests for logging redaction."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from workflow_runner.logging_utils import (
    configure_logging,
    get_logger,
    redact,
)


def test_redact_inline_secret() -> None:
    assert "password=***REDACTED***" in redact("login password=hunter2 ok")


def test_logger_redacts_extra(tmp_path: Path) -> None:
    log_file = tmp_path / "log.jsonl"
    configure_logging(level="DEBUG", log_file=log_file, quiet=True)
    log = get_logger("test")
    log.info("attempting login", extra={"password": "hunter2", "user": "alice"})
    logging.shutdown()
    lines = [line for line in log_file.read_text().splitlines() if line.strip()]
    assert lines, "expected at least one log line"
    record = json.loads(lines[-1])
    assert record["password"] == "***REDACTED***"
    assert record["user"] == "alice"


def test_redact_idempotent() -> None:
    once = redact("token=abc")
    twice = redact(once)
    assert once == twice
