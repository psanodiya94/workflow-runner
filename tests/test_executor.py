"""Tests for the executor layer: destructive detection and env handling."""

import pytest

from workflow_runner.executor.command import (
    build_env_prefix,
    is_destructive,
    sanitize_env,
)


class TestIsDestructive:
    def test_rm_flag(self):
        assert is_destructive("rm -rf /tmp/foo")

    def test_rm_plain(self):
        assert is_destructive("rm file.txt")

    def test_rmdir(self):
        assert is_destructive("rmdir /tmp/mydir")

    def test_dd(self):
        assert is_destructive("dd if=/dev/zero of=/dev/sda")

    def test_mkfs(self):
        assert is_destructive("mkfs.ext4 /dev/sdb1")

    def test_shutdown(self):
        assert is_destructive("sudo shutdown -h now")

    def test_reboot(self):
        assert is_destructive("reboot")

    def test_kill(self):
        assert is_destructive("kill -9 1234")

    def test_pkill(self):
        assert is_destructive("pkill nginx")

    def test_redirect_to_dev(self):
        assert is_destructive("echo 0 > /dev/sda")

    def test_safe_ls(self):
        assert not is_destructive("ls -la /tmp")

    def test_safe_echo(self):
        assert not is_destructive("echo hello world")

    def test_safe_grep(self):
        assert not is_destructive("grep -r pattern /var/log")

    def test_safe_systemctl_status(self):
        assert not is_destructive("systemctl status nginx")

    def test_safe_cat(self):
        assert not is_destructive("cat /etc/os-release")


class TestBuildEnvPrefix:
    def test_empty(self):
        assert build_env_prefix({}) == ""

    def test_single_var(self):
        prefix = build_env_prefix({"FOO": "bar"})
        assert prefix.startswith("env ")
        assert "FOO=bar" in prefix
        assert prefix.endswith(" ")

    def test_value_with_spaces(self):
        prefix = build_env_prefix({"MSG": "hello world"})
        assert "MSG='hello world'" in prefix or 'MSG="hello world"' in prefix

    def test_multiple_vars(self):
        prefix = build_env_prefix({"A": "1", "B": "2"})
        assert "A=1" in prefix
        assert "B=2" in prefix


class TestSanitizeEnv:
    def test_redacts_password(self):
        env = {"DB_PASSWORD": "supersecret", "HOST": "localhost"}
        result = sanitize_env(env)
        assert result["DB_PASSWORD"] == "***"
        assert result["HOST"] == "localhost"

    def test_redacts_token(self):
        env = {"API_TOKEN": "abc123"}
        assert sanitize_env(env)["API_TOKEN"] == "***"

    def test_redacts_key(self):
        env = {"PRIVATE_KEY": "-----BEGIN..."}
        assert sanitize_env(env)["PRIVATE_KEY"] == "***"

    def test_preserves_safe_vars(self):
        env = {"HOME": "/root", "PATH": "/usr/bin", "APP_DIR": "/opt/app"}
        result = sanitize_env(env)
        assert result == env

    def test_case_insensitive(self):
        env = {"MyPassword": "secret"}
        assert sanitize_env(env)["MyPassword"] == "***"
