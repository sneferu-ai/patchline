"""Config/env contract tests (FR-050/052/056/063)."""
from __future__ import annotations

from patchline import config


class TestAllowlist:
    def test_unset_and_empty_are_misconfigured(self, clean_env, monkeypatch):
        assert config.allowlisted_login() is None
        monkeypatch.setenv("GITHUB_ALLOWLISTED_LOGIN", "")
        assert config.allowlisted_login() is None
        monkeypatch.setenv("GITHUB_ALLOWLISTED_LOGIN", "   ")
        assert config.allowlisted_login() is None

    def test_reread_every_call(self, clean_env, monkeypatch):
        """FR-052: re-read per request — mid-flight changes take effect."""
        monkeypatch.setenv("GITHUB_ALLOWLISTED_LOGIN", "user-a")
        assert config.allowlisted_login() == "user-a"
        monkeypatch.setenv("GITHUB_ALLOWLISTED_LOGIN", "user-b")
        assert config.allowlisted_login() == "user-b"


class TestRescanInterval:
    def test_default_weekly(self, clean_env):
        assert config.rescan_interval_seconds() == 168 * 3600

    def test_seconds_override_wins(self, clean_env, monkeypatch):
        monkeypatch.setenv("RESCAN_INTERVAL_SECONDS", "2")
        assert config.rescan_interval_seconds() == 2

    def test_hours_env(self, clean_env, monkeypatch):
        monkeypatch.setenv("RESCAN_INTERVAL_HOURS", "24")
        assert config.rescan_interval_seconds() == 24 * 3600


class TestStepUp:
    def test_default_900(self, clean_env):
        assert config.step_up_age_seconds() == 900

    def test_override(self, clean_env, monkeypatch):
        monkeypatch.setenv("SESSION_STEP_UP_AGE_SECONDS", "1")
        assert config.step_up_age_seconds() == 1


class TestPrivateKey:
    def test_literal_newlines_converted(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "line1\\nline2\\n")
        assert config.github_app_private_key_pem() == "line1\nline2\n"

    def test_real_newlines_kept(self, clean_env, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "line1\nline2\n")
        assert config.github_app_private_key_pem() == "line1\nline2\n"

    def test_path_takes_precedence(self, clean_env, monkeypatch, tmp_path):
        key_file = tmp_path / "key.pem"
        key_file.write_text("from-file\n")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(key_file))
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "from-env\n")
        assert config.github_app_private_key_pem() == "from-file\n"


class TestDemoMode:
    def test_flag(self, clean_env, monkeypatch):
        assert config.demo_mode() is False
        monkeypatch.setenv("DEMO_MODE", "true")
        assert config.demo_mode() is True
