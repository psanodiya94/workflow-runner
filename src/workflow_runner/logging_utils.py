"""Structured logging configuration shared by every component.

The module exposes a single :func:`configure_logging` entry point so the CLI,
the workflow engine, and any embedding application all agree on log format and
verbosity. Logs are emitted as plain text to stderr by default and can be
mirrored to a per-session JSON-lines file for later inspection.

Sensitive fields (``password``, ``passphrase``, ``private_key``, ``token``,
``secret``) are scrubbed from extra context before being written.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_DEFAULT_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s :: %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

_REDACTED = "***REDACTED***"
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "private_key",
        "privatekey",
        "pkey",
        "secret",
        "token",
        "auth",
        "authorization",
        "api_key",
        "apikey",
    }
)
_SENSITIVE_PATTERN = re.compile(
    r"(?i)(password|passwd|passphrase|token|secret|api[_-]?key)\s*[:=]\s*\S+"
)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: (_REDACTED if k.lower() in _SENSITIVE_KEYS else _scrub_value(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        cleaned = [_scrub_value(item) for item in value]
        return type(value)(cleaned) if not isinstance(value, set) else set(cleaned)
    if isinstance(value, str):
        return _SENSITIVE_PATTERN.sub(lambda m: f"{m.group(1)}={_REDACTED}", value)
    return value


class _RedactingFilter(logging.Filter):
    """Strip sensitive substrings from messages and structured ``extra`` fields."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401 - logging hook
        if isinstance(record.msg, str):
            record.msg = _SENSITIVE_PATTERN.sub(
                lambda m: f"{m.group(1)}={_REDACTED}", record.msg
            )
        if record.args:
            record.args = tuple(_scrub_value(arg) for arg in record.args)
        for attr in list(vars(record)):
            if attr.lower() in _SENSITIVE_KEYS:
                setattr(record, attr, _REDACTED)
        return True


class _JsonLineFormatter(logging.Formatter):
    """Render each log record as a single JSON object on one line."""

    _BUILTIN = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "asctime", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, _DEFAULT_DATEFMT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._BUILTIN or key.startswith("_"):
                continue
            payload[key] = _scrub_value(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    level: str | int = "INFO",
    *,
    log_file: Path | str | None = None,
    quiet: bool = False,
    json_console: bool = False,
) -> None:
    """Configure the root logger for the application.

    Args:
        level: Logging level name or numeric level.
        log_file: Optional path to write a JSONL log of every record. Parent
            directories are created automatically.
        quiet: If True, suppress console output entirely.
        json_console: If True, emit JSONL on the console too (useful for
            piping into log shippers).
    """
    root = logging.getLogger()
    root.setLevel(level)
    # Wipe existing handlers — re-running ``configure_logging`` should be safe
    # even when called from tests.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    redactor = _RedactingFilter()

    if not quiet:
        console = logging.StreamHandler(stream=sys.stderr)
        console.setFormatter(
            _JsonLineFormatter() if json_console
            else logging.Formatter(_DEFAULT_FORMAT, _DEFAULT_DATEFMT)
        )
        console.addFilter(redactor)
        root.addHandler(console)

    if log_file is not None:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(_JsonLineFormatter())
        rotating.addFilter(redactor)
        root.addHandler(rotating)

    # paramiko is chatty at INFO; clamp it unless caller really wants debug.
    if isinstance(level, str):
        numeric_level = logging.getLevelName(level.upper())
    else:
        numeric_level = level
    if not isinstance(numeric_level, int) or numeric_level > logging.DEBUG:
        logging.getLogger("paramiko").setLevel(logging.WARNING)


class _ContextAdapter(logging.LoggerAdapter):
    """LoggerAdapter that *merges* ``extra`` rather than overriding it.

    The stdlib default replaces caller-supplied ``extra`` with the adapter's,
    which silently swallows per-call context. We merge: adapter context wins
    for shared keys (the adapter is treated as the more authoritative scope).
    """

    def process(self, msg, kwargs):  # type: ignore[override]
        caller_extra = kwargs.get("extra") or {}
        merged = {**caller_extra, **(self.extra or {})}
        kwargs["extra"] = merged
        return msg, kwargs


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """Return a :class:`LoggerAdapter` that injects ``context`` on every record."""
    return _ContextAdapter(logging.getLogger(name), _scrub_value(context))


def redact(text: str) -> str:
    """Public helper for callers that want to scrub ad-hoc strings."""
    return _SENSITIVE_PATTERN.sub(lambda m: f"{m.group(1)}={_REDACTED}", text)


def sensitive_keys() -> Iterable[str]:
    """Expose the redaction key list (mostly for tests)."""
    return iter(_SENSITIVE_KEYS)
