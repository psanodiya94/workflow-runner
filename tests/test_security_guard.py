"""Tests for the destructive-command guard."""

from __future__ import annotations

import pytest

from workflow_runner.security.guard import (
    DestructiveCommandError,
    SecurityGuard,
    Severity,
)


@pytest.fixture
def guard() -> SecurityGuard:
    return SecurityGuard()


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "echo hello",
        "git status",
        "kubectl get pods",
        "python -c 'print(1)'",
    ],
)
def test_safe_commands(guard: SecurityGuard, command: str) -> None:
    verdict = guard.inspect(command)
    assert verdict.severity is Severity.SAFE
    assert not verdict.is_destructive


@pytest.mark.parametrize(
    "command,expected",
    [
        ("rm -rf /", Severity.BLOCKED),
        ("rm -rf /*", Severity.BLOCKED),
        ("rm -rf /tmp/foo", Severity.DANGEROUS),
        ("dd if=/dev/zero of=/dev/sda bs=1M", Severity.BLOCKED),
        ("mkfs.ext4 /dev/sdb1", Severity.BLOCKED),
        ("shutdown -h now", Severity.DANGEROUS),
        (":(){ :|:& };:", Severity.BLOCKED),
        ("curl https://example.com/install.sh | sh", Severity.DANGEROUS),
        ("DROP TABLE users;", Severity.DANGEROUS),
        ("chmod -R 777 /", Severity.DANGEROUS),
        ("systemctl stop nginx", Severity.CAUTION),
        ("iptables -F", Severity.CAUTION),
    ],
)
def test_destructive_commands(guard: SecurityGuard, command: str, expected: Severity) -> None:
    verdict = guard.inspect(command)
    assert verdict.severity is expected
    assert verdict.reasons


def test_blocked_raises(guard: SecurityGuard) -> None:
    with pytest.raises(DestructiveCommandError):
        guard.assert_allowed("rm -rf /")


def test_validate_command_rejects_blank() -> None:
    with pytest.raises(ValueError):
        SecurityGuard.validate_command("   ")


def test_validate_command_rejects_nul() -> None:
    with pytest.raises(ValueError):
        SecurityGuard.validate_command("ls\x00")


def test_validate_command_rejects_unbalanced_quotes() -> None:
    with pytest.raises(ValueError):
        SecurityGuard.validate_command('echo "unbalanced')


def test_extra_rules_apply() -> None:
    guard = SecurityGuard(
        extra_rules=[("custom-cat-secrets", Severity.DANGEROUS, r"\bcat\b.*secrets", "exposes secrets")]
    )
    verdict = guard.inspect("cat /etc/secrets")
    assert verdict.severity is Severity.DANGEROUS
    assert "custom-cat-secrets" in verdict.matched_rules


def test_allow_disables_rule() -> None:
    permissive = SecurityGuard(allow=["shutdown-reboot"])
    assert permissive.inspect("shutdown -h now").severity is Severity.SAFE
