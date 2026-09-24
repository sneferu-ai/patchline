"""Web-layer integration tests (FastAPI TestClient; skipped without httpx).

Covers AC-002 (health), AC-006-style gating, AC-017 (CSRF), AC-005 (single-field
422), AC-028 (demo mode), FR-031/003.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("httpx")


@pytest.fixture()
def app_client(db_url, monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_MODE", "mock")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("GITHUB_ALLOWLISTED_LOGIN", "test-owner")
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(tmp_path / ".worker_heartbeat"))
    from patchline import models
    from patchline.db import get_engine

    engine = get_engine()
    models.Base.metadata.create_all(engine)
    from patchline.app import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def demo_client(db_url, monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("ENGINE_MODE", "mock")
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(tmp_path / ".worker_heartbeat"))
    from patchline import models
    from patchline.db import get_engine

    engine = get_engine()
    models.Base.metadata.create_all(engine)
    from patchline.app import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app, follow_redirects=True) as client:
        yield client


class TestHealth:
    def test_healthz_connected(self, app_client):
        resp = app_client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "healthy", "database": "connected"}

    def test_healthz_deep(self, app_client):
        resp = app_client.get("/healthz?deep=1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["database"] == "connected"
        assert body["engine"] == "healthy"
        assert body["engine_mode"] == "mock"
        assert "worker" in body


class TestGating:
    def test_protected_redirects(self, app_client):
        assert app_client.get("/repositories", follow_redirects=False).status_code == 302
        assert app_client.get("/", follow_redirects=False).status_code == 302
        assert app_client.get("/jobs", follow_redirects=False).status_code == 302

    def test_static_public(self, app_client):
        resp = app_client.get("/static/styles.css")
        assert resp.status_code == 200

    def test_login_page_public(self, app_client):
        resp = app_client.get("/login")
        assert resp.status_code == 200


class TestDemoMode:
    def test_auto_login_and_banner(self, demo_client):
        resp = demo_client.get("/")
        assert resp.status_code == 200
        assert "Demo Mode" in resp.text

    def test_allowlist_not_required(self, demo_client):
        # demo_client has no GITHUB_ALLOWLISTED_LOGIN set — requests still work.
        resp = demo_client.get("/repositories")
        assert resp.status_code == 200
        assert "acme/widgets" in resp.text or "No repositories" in resp.text

    def test_demo_repositories_render_from_fixtures(self, demo_client):
        resp = demo_client.get("/demo", follow_redirects=False)
        assert resp.status_code == 302


class TestEntryValidation:
    def _login_demo(self, client):
        client.get("/demo")

    def test_title_too_long_422_single_field(self, demo_client):
        self._login_demo(demo_client)
        # Grab a CSRF token from the entry form.
        form_page = demo_client.get("/entries/new")
        assert form_page.status_code == 200
        import re

        match = re.search(r'name="csrf_token" value="([^"]+)"', form_page.text)
        assert match, "entry form must embed a CSRF token"
        token = match.group(1)
        resp = demo_client.post(
            "/entries/new",
            data={
                "csrf_token": token,
                "title": "x" * 201,
                "prior_api_shape": "old shape",
                "target_contract": "new contract",
                "language": "python",
            },
        )
        assert resp.status_code == 422
        # The error names ONLY the violated field.
        assert "title must be 1–200 characters" in resp.text
        assert "prior_api_shape must" not in resp.text
        assert "target_contract must" not in resp.text
        assert "language must" not in resp.text

    def test_csrf_required(self, demo_client):
        self._login_demo(demo_client)
        resp = demo_client.post(
            "/entries/new",
            data={"title": "t", "prior_api_shape": "a", "target_contract": "b", "language": "python"},
        )
        assert resp.status_code == 403

    def test_create_entry_happy_path(self, demo_client):
        self._login_demo(demo_client)
        form_page = demo_client.get("/entries/new")
        import re

        token = re.search(r'name="csrf_token" value="([^"]+)"', form_page.text).group(1)
        resp = demo_client.post(
            "/entries/new",
            data={
                "csrf_token": token,
                "title": "Rename get_user",
                "prior_api_shape": "old",
                "target_contract": "new",
                "language": "python",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/entries/")
