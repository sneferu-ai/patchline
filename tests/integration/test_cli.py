"""CLI tests (FR-029/049/051/052/064, AC-023/034/036/041/042 support)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("sqlalchemy")

from patchline import cli

from conftest import make_entry, make_vendor


class TestVerifyConfig:
    def _base_env(self, monkeypatch):
        for name, value in {
            "GITHUB_APP_ID": "app-1",
            "GITHUB_APP_SLUG": "patchline",
            "GITHUB_CLIENT_ID": "cid",
            "GITHUB_CLIENT_SECRET": "csec",
            "GITHUB_WEBHOOK_SECRET": "whsec",
            "GITHUB_ALLOWLISTED_LOGIN": "owner",
            "ENGINE_MODE": "mock",
            "SESSION_SECRET": "ssec",
            "DATABASE_URL": "sqlite:///x.db",
            "DEFAULT_LANGUAGE": "python",
            "GITHUB_APP_PRIVATE_KEY": "pem",
        }.items():
            monkeypatch.setenv(name, value)

    def test_ok(self, clean_env, monkeypatch):
        self._base_env(monkeypatch)
        errors, _warnings = cli.verify_config_errors()
        assert errors == []

    def test_missing_vars_reported(self, clean_env):
        errors, _ = cli.verify_config_errors()
        assert any("GITHUB_ALLOWLISTED_LOGIN" in e for e in errors)
        assert any("SESSION_SECRET" in e for e in errors)

    def test_empty_allowlist_is_error(self, clean_env, monkeypatch):
        self._base_env(monkeypatch)
        monkeypatch.setenv("GITHUB_ALLOWLISTED_LOGIN", "")
        errors, _ = cli.verify_config_errors()
        assert any("allowlist" in e.lower() for e in errors)

    def test_notification_pairing(self, clean_env, monkeypatch):
        """AC-023(f): exactly one of URL/secret set → pairing error."""
        self._base_env(monkeypatch)
        monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "https://receiver.example/hook")
        errors, _ = cli.verify_config_errors()
        assert any("NOTIFICATION" in e and "together" in e for e in errors)

    def test_sneferu_mode_requires_endpoint(self, clean_env, monkeypatch):
        self._base_env(monkeypatch)
        monkeypatch.setenv("ENGINE_MODE", "sneferu")
        errors, _ = cli.verify_config_errors()
        assert any("SNEFERU_BASE_URL" in e for e in errors)
        assert any("SNEFERU_SERVICE_TOKEN" in e for e in errors)


class TestDestructiveGate:
    def test_add_column_not_destructive(self):
        sources = [("0002_add.py", "def upgrade():\n    op.add_column('t', sa.Column('c', sa.Integer()))\n")]
        assert cli._has_destructive_ops(sources) == []

    def test_drop_table_flagged(self):
        sources = [("0003_drop.py", "def upgrade():\n    op.drop_table('legacy')\n")]
        assert cli._has_destructive_ops(sources) == ["0003_drop.py"]
        sources_sql = [("0004_drop.py", "op.execute('DROP TABLE legacy')")]
        assert cli._has_destructive_ops(sources_sql) == ["0004_drop.py"]
        sources_col = [("0005_drop.py", "op.execute('ALTER TABLE t DROP COLUMN c')")]
        assert cli._has_destructive_ops(sources_col) == ["0005_drop.py"]


class TestSetDefaultLanguage:
    def test_updates_config(self, db, sess, capsys):
        rc = cli.main(["set-default-language", "python"])
        assert rc == 0
        from patchline.core.config_store import default_language

        assert default_language(sess) == "python"

    def test_rejects_uninstalled_pack(self, db, capsys):
        rc = cli.main(["set-default-language", "go"])
        assert rc == 1


class TestRevokeSessions:
    def test_revokes_all(self, db, sess, capsys):
        from patchline import auth

        vendor = make_vendor(sess)
        auth.create_vendor_session(sess, vendor.id)
        auth.create_vendor_session(sess, vendor.id)
        sess.commit()
        rc = cli.main(["revoke-sessions"])
        assert rc == 0
        assert "revoked 2" in capsys.readouterr().out


class TestCleanup:
    def test_exemptions(self, db, sess, capsys):
        """AC-041/FR-049: feedback flags + confirmations survive cleanup."""
        from patchline.models import ChangelogEntry, Confirmation, FeedbackFlag, Occurrence, ScanJob

        vendor = make_vendor(sess)
        old_entry = make_entry(sess, vendor, title="old", status="ready")
        old_entry.created_at = datetime.now(timezone.utc) - timedelta(days=400)
        job = ScanJob(entry_id=old_entry.id, repository_id=1, branch="main", status="done",
                      created_at=datetime.now(timezone.utc) - timedelta(days=400))
        sess.add(job)
        sess.flush()
        occ = Occurrence(scan_job_id=job.id, file_path="a.py", line_start=1, line_end=1, snippet="x",
                         )
        sess.add(occ)
        sess.flush()
        flag = FeedbackFlag(occurrence_id=occ.id, vendor_id=vendor.id, reason="fp")
        conf = Confirmation(remediation_id=1, entry_id=old_entry.id, repository_id=1, vendor_id=vendor.id, diff_sha256="abc")
        sess.add_all([flag, conf])
        sess.commit()

        rc = cli.main(["cleanup", "--before", "2020-01-01"])
        assert rc == 0
        sess.expire_all()
        assert sess.get(FeedbackFlag, flag.id).deleted_at is None  # exempt
        assert sess.get(Confirmation, conf.id) is not None  # never deleted

        rc2 = cli.main(["cleanup", "--before", "2099-01-01"])
        assert rc2 == 0
        sess.expire_all()
        assert sess.get(FeedbackFlag, flag.id).deleted_at is None  # STILL exempt
        assert sess.get(ChangelogEntry, old_entry.id).deleted_at is not None
        # Occurrence has no created_at — cleanup joins to ScanJob.created_at.
        assert sess.get(Occurrence, occ.id).deleted_at is not None
        # ScanJob itself is soft-deleted (has created_at).
        assert sess.get(ScanJob, job.id).deleted_at is not None


class TestExportPilotEvidence:
    def test_schema_and_counts(self, db, sess, capsys):
        """AC-023(c): export status_counts match SQL aggregation."""
        from patchline.core.coverage import apply_transition

        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        from conftest import make_repo

        repo = make_repo(sess, full_name="acme/widgets")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="scanning")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="no_hits")
        sess.commit()
        rc = cli.main(["export-pilot-evidence"])
        assert rc == 0
        export = json.loads(capsys.readouterr().out)
        assert export["instance"]["app_version"]
        assert export["instance"]["engine_mode"] == "mock"
        counts = export["coverage_summary"]["status_counts"]
        assert counts["no_hits"] == 1
        assert counts["scanning"] == 0
        assert export["coverage_summary"]["total_entries"] == 1
        assert export["entries"][0]["title"] == entry.title
        assert "confirmations" in export and "webhook_deliveries" in export
