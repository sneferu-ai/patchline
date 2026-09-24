"""Job queue tests (FR-012/014/040/048/063/070, AC-039/044 support)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("sqlalchemy")

from patchline.core import jobs
from patchline.core.jobs import (
    PRIORITY_RESCAN,
    PRIORITY_USER,
    claim_next_job,
    configured_branches,
    enqueue_rescan,
    enqueue_scans_for_entry,
    enqueue_verification,
    reenqueue_rate_limited,
    requeue_stale_jobs,
)

from conftest import make_entry, make_repo, make_vendor


class TestConfiguredBranches:
    def test_default_branch(self, sess):
        vendor = make_vendor(sess)
        repo = make_repo(sess, full_name="a/b")
        assert configured_branches(repo) == ["main"]

    def test_json_branches(self, sess):
        import json

        vendor = make_vendor(sess)
        repo = make_repo(sess, full_name="a/b")
        repo.scan_branches = json.dumps(["main", "release-1.0"])
        assert configured_branches(repo) == ["main", "release-1.0"]

    def test_capped_at_ten(self, sess):
        import json

        vendor = make_vendor(sess)
        repo = make_repo(sess, full_name="a/b")
        repo.scan_branches = json.dumps([f"b{i}" for i in range(15)])
        assert len(configured_branches(repo)) == 10


class TestEnqueue:
    def test_one_job_per_branch_default_first(self, sess):
        import json

        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        repo.scan_branches = json.dumps(["release-1.0", "main"])  # unordered on purpose
        created = enqueue_scans_for_entry(sess, entry, [repo], priority=PRIORITY_USER)
        assert len(created) == 2
        assert created[0].branch == "main"  # default branch first
        assert created[1].branch == "release-1.0"
        # FR-040: default-branch job outranks non-default for the same repo.
        assert created[0].priority == PRIORITY_USER
        assert created[1].priority == PRIORITY_USER - 1

    def test_dedup_on_repeated_trigger(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        first = enqueue_scans_for_entry(sess, entry, [repo])
        second = enqueue_scans_for_entry(sess, entry, [repo])
        assert len(first) == 1
        assert second == []  # already queued

    def test_verification_dedup(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        j1 = enqueue_verification(sess, entry, repo, delay_seconds=0)
        j2 = enqueue_verification(sess, entry, repo, delay_seconds=0)
        assert j1.id == j2.id

    def test_language_mismatch_skipped(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor, language="python")
        go_repo = make_repo(sess, full_name="acme/gadgets", language="go")
        created = enqueue_scans_for_entry(sess, entry, [go_repo])
        assert len(created) == 1
        assert created[0].status == "skipped_language"
        from patchline.models import CoverageState

        cov = sess.query(CoverageState).filter_by(entry_id=entry.id, repository_id=go_repo.id).one()
        assert cov.status == "skipped_language"

    def test_inactive_repo_skipped(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        repo.connection_state = "removed"
        assert enqueue_scans_for_entry(sess, entry, [repo]) == []

    def test_match_enters_scanning(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        enqueue_scans_for_entry(sess, entry, [repo])
        from patchline.models import CoverageState

        cov = sess.query(CoverageState).filter_by(entry_id=entry.id, repository_id=repo.id).one()
        assert cov.status == "scanning"


class TestClaim:
    def test_priority_order_then_fifo(self, sess):
        """AC-039: user scan (priority=10) beats an earlier-created rescan (0)."""
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        from patchline.models import ScanJob

        older = ScanJob(entry_id=entry.id, repository_id=repo.id, branch="main", priority=PRIORITY_RESCAN, status="queued",
                        created_at=datetime.now(timezone.utc) - timedelta(hours=1))
        newer = ScanJob(entry_id=entry.id, repository_id=repo.id, branch="main", priority=PRIORITY_USER, status="queued")
        sess.add_all([older, newer])
        sess.flush()
        claimed = claim_next_job(sess)
        assert claimed.id == newer.id
        assert claimed.status == "running"
        # Atomic claim: a second claim of the same job cannot succeed.
        claimed2 = claim_next_job(sess)
        assert claimed2.id == older.id

    def test_next_attempt_at_skip(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        from patchline.models import ScanJob

        future = ScanJob(
            entry_id=entry.id,
            repository_id=repo.id,
            branch="main",
            priority=99,
            status="queued",
            next_attempt_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        sess.add(future)
        sess.flush()
        assert claim_next_job(sess) is None

    def test_rate_limit_reenqueue(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        enqueue_scans_for_entry(sess, entry, [repo])
        job = claim_next_job(sess)
        reenqueue_rate_limited(sess, job, 120.0)
        assert job.status == "queued"
        assert job.next_attempt_at is not None
        assert claim_next_job(sess) is None  # deferred; queue not blocked by it

    def test_stale_claim_requeued(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        enqueue_scans_for_entry(sess, entry, [repo])
        job = claim_next_job(sess)
        job.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        sess.flush()
        assert requeue_stale_jobs(sess) == 1
        sess.expire(job)
        assert job.status == "queued"
        assert job.claimed_at is None


class TestVerificationEnqueue:
    def test_verification_does_not_transition_coverage(self, sess):
        """FR-024/FR-065: pr_open stays until the verification result lands."""
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor)
        repo = make_repo(sess, full_name="acme/widgets")
        from patchline.core.coverage import apply_transition
        from patchline.models import CoverageState

        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="scanning")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="pattern_detected")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="remediation_pending")
        apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status="pr_open")
        job = enqueue_verification(sess, entry, repo, delay_seconds=5)
        assert job.purpose == "verification"
        assert job.next_attempt_at is not None
        cov = sess.query(CoverageState).filter_by(entry_id=entry.id, repository_id=repo.id).one()
        assert cov.status == "pr_open"  # unchanged
