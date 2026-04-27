"""Command validation helpers: destructive-pattern detection and env handling."""

from __future__ import annotations

import re
import shlex

# Patterns that warrant an explicit confirmation prompt before execution.
# Matches against the raw command string (not tokenised) so aliases and
# quoting don't bypass the check.
_DESTRUCTIVE_PATTERNS: list[str] = [
    r"\brm\b",
    r"\brmdir\b",
    r"\bdd\b",
    r"\bmkfs\b",
    r"\bfdisk\b",
    r"\bparted\b",
    r"\bshred\b",
    r"\btruncate\b",
    r"\bwipefs\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\bkill\b",
    r"\bpkill\b",
    r"\bkillall\b",
    r"\bdropdb\b",
    r"\bdropdatabase\b",
    r"> /dev/",
]

_COMPILED = [re.compile(p) for p in _DESTRUCTIVE_PATTERNS]


def is_destructive(command: str) -> bool:
    """Return True if *command* matches any known destructive pattern."""
    for pat in _COMPILED:
        if pat.search(command):
            return True
    return False


def build_env_prefix(env: dict[str, str]) -> str:
    """
    Build a portable ``env KEY=VALUE …`` prefix for injecting environment
    variables into a remote command without relying on sshd's AcceptEnv.
    Returns an empty string if *env* is empty.
    """
    if not env:
        return ""
    pairs = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    return f"env {pairs} "


def sanitize_env(env: dict[str, str]) -> dict[str, str]:
    """
    Return a copy of *env* with sensitive values replaced by ``***``.
    Used only for logging — the original env is passed to the remote host.
    """
    _SENSITIVE_KEYS = {"password", "passwd", "secret", "token", "key", "credential", "auth"}
    return {
        k: ("***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else v)
        for k, v in env.items()
    }
