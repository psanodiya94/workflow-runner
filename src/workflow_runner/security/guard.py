"""Lightweight static analysis for shell commands.

The guard does *not* try to be a full shell parser — that's an arms race we
will lose. Instead, it focuses on a curated list of high-blast-radius
patterns (``rm -rf /``, ``mkfs``, ``shutdown`` ...) so the runtime can prompt
the user before executing them.

Two concepts:

* :class:`SecurityVerdict` — what the guard found (severity, reason, matched
  rule). Always returned, never raised.
* :class:`DestructiveCommandError` — raised by the executor when a destructive
  command is blocked outright (``policy="deny"``).

The CLI translates verdicts into interactive confirmations; the workflow
engine consults each step's ``confirm`` flag to decide whether to prompt or
abort automatically.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class _Rule:
    name: str
    severity: Severity
    pattern: re.Pattern[str]
    reason: str


@dataclass(frozen=True)
class SecurityVerdict:
    """Outcome of inspecting a single command string."""

    severity: Severity
    reasons: tuple[str, ...] = field(default_factory=tuple)
    matched_rules: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_destructive(self) -> bool:
        return self.severity in (Severity.DANGEROUS, Severity.BLOCKED)

    @property
    def requires_confirmation(self) -> bool:
        return self.severity in (Severity.CAUTION, Severity.DANGEROUS)


class DestructiveCommandError(RuntimeError):
    """Raised when a command is blocked by policy."""

    def __init__(self, command: str, verdict: SecurityVerdict):
        self.command = command
        self.verdict = verdict
        reason = "; ".join(verdict.reasons) or "blocked by security policy"
        super().__init__(f"Refusing to run command: {reason}")


# Ordering: more specific rules first so their reasons land in the verdict.
_BUILTIN_RULES: tuple[_Rule, ...] = (
    _Rule(
        name="rm-rf-root",
        severity=Severity.BLOCKED,
        pattern=re.compile(
            r"\brm\b[^|;&\n]*\s-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+(?:/|/\*)(?:\s|$)"
        ),
        reason="`rm -rf /` (or equivalent) would wipe the entire filesystem",
    ),
    _Rule(
        name="rm-recursive-force",
        severity=Severity.DANGEROUS,
        pattern=re.compile(r"\brm\b[^|;&\n]*\s-[a-zA-Z]*r[a-zA-Z]*f"),
        reason="recursive forced delete (`rm -rf`)",
    ),
    _Rule(
        name="dd-of-device",
        severity=Severity.BLOCKED,
        pattern=re.compile(r"\bdd\b[^|;&\n]*\bof=/dev/(sd[a-z]|nvme|hd[a-z]|xvd[a-z])"),
        reason="`dd` writing to a block device will destroy data",
    ),
    _Rule(
        name="mkfs",
        severity=Severity.BLOCKED,
        pattern=re.compile(r"\bmkfs(\.\w+)?\b"),
        reason="formatting a filesystem is destructive",
    ),
    _Rule(
        name="fdisk-parted",
        severity=Severity.DANGEROUS,
        pattern=re.compile(r"\b(fdisk|parted|sgdisk|wipefs)\b"),
        reason="partition table manipulation",
    ),
    _Rule(
        name="shutdown-reboot",
        severity=Severity.DANGEROUS,
        pattern=re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b"),
        reason="will power off or reboot the host",
    ),
    _Rule(
        name="kill-pid1",
        severity=Severity.DANGEROUS,
        pattern=re.compile(r"\bkill(all)?\b[^|;&\n]*\s(-9\s+)?1(\s|$)"),
        reason="killing PID 1 will crash the system",
    ),
    _Rule(
        name="fork-bomb",
        severity=Severity.BLOCKED,
        pattern=re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
        reason="classic fork-bomb",
    ),
    _Rule(
        name="curl-pipe-shell",
        severity=Severity.DANGEROUS,
        pattern=re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b"),
        reason="piping remote content into a shell",
    ),
    _Rule(
        name="chmod-777-root",
        severity=Severity.DANGEROUS,
        pattern=re.compile(r"\bchmod\b[^|;&\n]*\s-?R?\s*777\s+/"),
        reason="recursive world-writable on /",
    ),
    _Rule(
        name="chown-root",
        severity=Severity.CAUTION,
        pattern=re.compile(r"\bchown\b[^|;&\n]*\s-R\s+\S+\s+/(\s|$)"),
        reason="recursive ownership change at filesystem root",
    ),
    _Rule(
        name="iptables-flush",
        severity=Severity.CAUTION,
        pattern=re.compile(r"\biptables\b[^|;&\n]*\s-F\b"),
        reason="flushing firewall rules",
    ),
    _Rule(
        name="drop-database",
        severity=Severity.DANGEROUS,
        pattern=re.compile(r"(?i)\bdrop\s+(database|schema|table)\b"),
        reason="SQL drop statement",
    ),
    _Rule(
        name="systemctl-stop-critical",
        severity=Severity.CAUTION,
        pattern=re.compile(r"\bsystemctl\b[^|;&\n]*\s(stop|disable|mask)\b"),
        reason="stopping or disabling a systemd unit",
    ),
)


class SecurityGuard:
    """Inspect commands for destructive intent.

    The guard is intentionally stateless and side-effect-free. Custom rules can
    be added at construction time — that's the extension point used by tests
    and the workflow loader's per-step ``patterns`` field.
    """

    def __init__(
        self,
        *,
        extra_rules: Iterable[tuple[str, Severity, str, str]] = (),
        allow: Iterable[str] = (),
    ) -> None:
        rules = list(_BUILTIN_RULES)
        for name, severity, pattern, reason in extra_rules:
            rules.append(_Rule(name, Severity(severity), re.compile(pattern), reason))
        allowed = {a.lower() for a in allow}
        self._rules: tuple[_Rule, ...] = tuple(r for r in rules if r.name not in allowed)

    def inspect(self, command: str) -> SecurityVerdict:
        """Return a :class:`SecurityVerdict` for the given command string."""
        if not command or not command.strip():
            return SecurityVerdict(Severity.SAFE)
        reasons: list[str] = []
        matched: list[str] = []
        worst = Severity.SAFE
        for rule in self._rules:
            if rule.pattern.search(command):
                matched.append(rule.name)
                reasons.append(rule.reason)
                if _severity_rank(rule.severity) > _severity_rank(worst):
                    worst = rule.severity
        return SecurityVerdict(worst, tuple(reasons), tuple(matched))

    def assert_allowed(self, command: str) -> SecurityVerdict:
        """Raise :class:`DestructiveCommandError` if the command is blocked."""
        verdict = self.inspect(command)
        if verdict.severity is Severity.BLOCKED:
            raise DestructiveCommandError(command, verdict)
        return verdict

    @staticmethod
    def validate_command(command: str) -> str:
        """Reject obviously malformed input before sending it over SSH.

        This catches stray NUL bytes and blank commands. Quoting *inside* the
        command is the user's responsibility — we simply confirm shlex can
        tokenize it (POSIX mode) so we fail early instead of on the remote.
        """
        if "\x00" in command:
            raise ValueError("command contains a NUL byte")
        stripped = command.strip()
        if not stripped:
            raise ValueError("command is empty")
        try:
            shlex.split(stripped, posix=True)
        except ValueError as exc:
            raise ValueError(f"command failed to tokenize: {exc}") from exc
        return stripped


def _severity_rank(s: Severity) -> int:
    return {Severity.SAFE: 0, Severity.CAUTION: 1, Severity.DANGEROUS: 2, Severity.BLOCKED: 3}[s]
