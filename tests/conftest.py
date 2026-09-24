"""Shared pytest fixtures.

Import discipline: this module imports ONLY stdlib + pytest at top level so
collection is clean in minimal sandboxes. SQLAlchemy/httpx-dependent fixtures
import lazily and skip when unavailable.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(REPO_ROOT, "src")
FIXTURES_DIR = os.path.dirname(__file__)

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
if FIXTURES_DIR not in sys.path:  # tests/ itself: enables `from conftest import …`
    sys.path.insert(0, FIXTURES_DIR)


@pytest.fixture()
def repo_root() -> str:
    return REPO_ROOT


@pytest.fixture()
def fixtures_dir() -> str:
    return os.path.join(FIXTURES_DIR, "fixtures")


@pytest.fixture()
def engine_fixtures_dir(fixtures_dir) -> str:
    return os.path.join(fixtures_dir, "engine")


@pytest.fixture()
def github_fixtures_dir(fixtures_dir) -> str:
    return os.path.join(fixtures_dir, "github")


@pytest.fixture()
def clean_env(monkeypatch):
    """Strip every Patchline-relevant env var so each test starts clean."""
    for name in (
        "DEMO_MODE",
        "ENGINE_MODE",
        "DATABASE_URL",
        "GITHUB_ALLOWLISTED_LOGIN",
        "GITHUB_APP_ID",
        "GITHUB_APP_SLUG",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "GITHUB_WEBHOOK_SECRET",
        "SESSION_SECRET",
        "DEFAULT_LANGUAGE",
        "NOTIFICATION_WEBHOOK_URL",
        "NOTIFICATION_SIGNING_SECRET",
        "NOTIFICATION_BATCH_WINDOW_SECONDS",
        "INSTANCE_ID",
        "RESCAN_INTERVAL_HOURS",
        "RESCAN_INTERVAL_SECONDS",
        "SESSION_STEP_UP_AGE_SECONDS",
        "ALLOW_DESTRUCTIVE_MIGRATIONS",
        "MAX_REPOS_PER_INSTALLATION",
        "WORKER_HEARTBEAT_PATH",
        "SNEFERU_BASE_URL",
        "SNEFERU_SERVICE_TOKEN",
        "ENGINE_WORKFLOW_DERIVE",
        "ENGINE_WORKFLOW_SCAN",
        "ENGINE_WORKFLOW_REMEDIATE",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture()
def db_url(tmp_path, clean_env, monkeypatch) -> str:
    """Isolated sqlite database per test; resets the engine registry."""
    pytest.importorskip("sqlalchemy")
    url = f"sqlite:///{tmp_path}/patchline-test.db"
    monkeypatch.setenv("DATABASE_URL", url)
    from patchline.db import reset_engines

    reset_engines()
    yield url
    reset_engines()


@pytest.fixture()
def db(db_url):
    """Migrated database (schema via metadata, equivalent to alembic 0001)."""
    from patchline import models
    from patchline.db import get_engine

    engine = get_engine()
    models.Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def sess(db):
    """Open ORM session (caller closes)."""
    from patchline.db import session_factory

    session = session_factory()()
    yield session
    session.close()


def make_vendor(sess, login="test-owner", github_user_id=7):
    from patchline.models import Vendor

    vendor = Vendor(github_user_id=github_user_id, github_login=login)
    sess.add(vendor)
    sess.flush()
    return vendor


def make_repo(sess, installation=None, full_name="acme/widgets", language="python", default_branch="main"):
    from patchline.models import Installation, Repository

    if installation is None:
        installation = Installation(
            github_installation_id=99001,
            account_login=full_name.split("/", 1)[0],
            account_type="Organization",
            connection_state="active",
        )
        sess.add(installation)
        sess.flush()
    repo = Repository(
        installation_id=installation.id,
        full_name=full_name,
        default_branch=default_branch,
        primary_language=language,
        connection_state="active",
    )
    sess.add(repo)
    sess.flush()
    return repo


def make_entry(sess, vendor, title="Rename get_user", language="python", status="ready"):
    from patchline.models import ChangelogEntry

    entry = ChangelogEntry(
        vendor_id=vendor.id,
        title=title,
        prior_api_shape="The function get_user(id) accepts a numeric user ID.",
        target_contract="The function is now get_user_by_id(id).",
        language=language,
        status=status,
        slug=None,
    )
    sess.add(entry)
    sess.flush()
    entry.slug = entry.slug or f"entry-{entry.id}"
    sess.flush()
    return entry
