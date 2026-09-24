"""Access-rule unit tests (FR-003): public path table + step-up + validity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from patchline.auth import is_public_path, needs_step_up, session_invalid_reason


class TestPublicPaths:
    def test_public(self, clean_env):
        for path in ("/login", "/login/start", "/logout", "/auth/github/callback", "/healthz", "/webhooks/github"):
            assert is_public_path(path), path
        assert is_public_path("/static/styles.css")

    def test_protected(self, clean_env):
        for path in ("/", "/repositories", "/entries/new", "/jobs", "/settings", "/metrics", "/pull-requests"):
            assert not is_public_path(path), path

    def test_demo_only_when_demo_mode(self, clean_env, monkeypatch):
        assert not is_public_path("/demo")
        monkeypatch.setenv("DEMO_MODE", "true")
        assert is_public_path("/demo")


class TestValidity:
    def _session(self, **overrides):
        now = datetime.now(timezone.utc)
        base = dict(revoked_at=None, is_demo=False, expires_at=None, last_activity_at=now, created_at=now)
        base.update(overrides)
        return NS(**base)

    def test_valid(self):
        assert session_invalid_reason(self._session()) is None

    def test_revoked(self):
        assert session_invalid_reason(self._session(revoked_at=datetime.now(timezone.utc))) == "revoked"

    def test_idle_7_days(self):
        old = datetime.now(timezone.utc) - timedelta(days=8)
        assert "idle" in session_invalid_reason(self._session(last_activity_at=old))

    def test_demo_24h_absolute(self):
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        reason = session_invalid_reason(self._session(is_demo=True, created_at=old, last_activity_at=datetime.now(timezone.utc)))
        assert "demo" in reason
        # A fresh demo session is valid even with recent activity.
        assert session_invalid_reason(self._session(is_demo=True)) is None


class TestStepUp:
    def test_fresh_session_no_step_up(self, clean_env):
        session = NS(is_demo=False, last_activity_at=datetime.now(timezone.utc))
        assert needs_step_up(session) is False

    def test_old_activity_requires_step_up(self, clean_env):
        session = NS(is_demo=False, last_activity_at=datetime.now(timezone.utc) - timedelta(minutes=16))
        assert needs_step_up(session) is True

    def test_demo_noop(self, clean_env):
        session = NS(is_demo=True, last_activity_at=datetime.now(timezone.utc) - timedelta(hours=5))
        assert needs_step_up(session) is False

    def test_test_override(self, clean_env, monkeypatch):
        monkeypatch.setenv("SESSION_STEP_UP_AGE_SECONDS", "1")
        session = NS(is_demo=False, last_activity_at=datetime.now(timezone.utc) - timedelta(seconds=2))
        assert needs_step_up(session) is True
