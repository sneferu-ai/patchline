"""Coverage DB transitions + audit log (FR-065, AC-030)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("sqlalchemy")

from patchline.core.coverage import InvalidTransition, apply_transition

from conftest import make_entry, make_repo, make_vendor


def _cov(sess, entry_id, repo_id):
    from patchline.models import CoverageState

    return sess.query(CoverageState).filter_by(entry_id=entry_id, repository_id=repo_id).one()


class TestTransitions:
    def test_full_happy_path_chain(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        for target in ("scanning", "pattern_detected", "remediation_pending", "pr_open"):
            apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status=target)
        now = datetime.now(timezone.utc)
        apply_transition(
            sess, entry_id=entry.id, repository_id=repo.id, to_status="migrated", verification_timestamp=now
        )
        row = _cov(sess, entry.id, repo.id)
        assert row.status == "migrated"
        assert row.verification_timestamp is not None
        assert row.manual_migrated is False

    def test_invalid_transition_raises_and_keeps_state(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        with pytest.raises(InvalidTransition):
            apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="migrated")
        assert _cov(sess, entry.id, repo.id).status == "not_scanned"

    def test_joblog_audit_rows(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="scanning")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="no_hits")
        from patchline.models import JobLog

        logs = sess.query(JobLog).filter_by(job_type="coverage").all()
        assert len(logs) == 2
        assert logs[0].status == "scanning"
        assert logs[1].status == "no_hits"
        assert "not_scanned -> scanning" in logs[0].detail

    def test_manual_migrated_flag(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="scanning")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="pattern_detected")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="remediation_pending")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="dispatch_failed")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="migrated", manual_migrated=True)
        row = _cov(sess, entry.id, repo.id)
        assert row.manual_migrated is True

    def test_idempotent_noop(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="scanning")
        # Same target again: no error, no duplicate audit row.
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="scanning")
        from patchline.models import JobLog

        assert sess.query(JobLog).count() == 1

    def test_regression_cycle(self, sess):
        """AC-030: migrated → pattern_detected via periodic rescan regression."""
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        for target in ("scanning", "no_hits"):
            apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status=target)
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="pattern_detected")
        assert _cov(sess, entry.id, repo.id).status == "pattern_detected"
