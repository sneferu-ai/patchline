"""Webhook handler tests (FR-005/006/024/035/036/062, AC-014/019/035 support)."""
from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from patchline.adapters.scm.mock_github import FixtureGitHubClient
from patchline.core.webhook_handlers import delivery_already_succeeded, process_webhook

from conftest import make_entry, make_vendor


def _installation_payload(action="created"):
    return {
        "action": action,
        "installation": {"id": 77001, "account": {"login": "acme", "type": "Organization"}},
        "repositories": [{"full_name": "acme/widgets", "default_branch": "main"}],
    }


class TestInstallationCreated:
    def test_persists_installation_repo_and_autoscan(self, db, sess, github_fixtures_dir):
        """AC-035: new repo auto-enqueued for scan against existing ready entries."""
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor, status="ready")
        sess.commit()
        scm = FixtureGitHubClient(fixture_dir=github_fixtures_dir)
        result = process_webhook("del-1", "installation", _installation_payload(), scm)
        assert result == "processed"
        from patchline.models import Installation, Repository, ScanJob

        inst = sess.query(Installation).filter_by(github_installation_id=77001).one()
        assert inst.account_login == "acme"
        repos = sess.query(Repository).filter_by(installation_id=inst.id).all()
        names = {r.full_name for r in repos}
        assert "acme/widgets" in names
        jobs = sess.query(ScanJob).filter_by(entry_id=entry.id).all()
        assert jobs, "auto-scan jobs must be enqueued for ready entries (FR-062)"
        assert all(j.priority == 10 for j in jobs)

    def test_idempotent_skip(self, db, sess, github_fixtures_dir):
        """AC-019: a delivery recorded succeeded is skipped (survives restarts)."""
        scm = FixtureGitHubClient(fixture_dir=github_fixtures_dir)
        assert process_webhook("del-dup", "installation", _installation_payload(), scm) == "processed"
        assert delivery_already_succeeded("del-dup") is True
        assert process_webhook("del-dup", "installation", _installation_payload(), scm) == "skipped"
        from patchline.models import Installation

        assert sess.query(Installation).filter_by(github_installation_id=77001).count() == 1

    def test_deleted_marks_removed(self, db, sess, github_fixtures_dir):
        scm = FixtureGitHubClient(fixture_dir=github_fixtures_dir)
        process_webhook("del-2", "installation", _installation_payload(), scm)
        process_webhook("del-3", "installation", _installation_payload("deleted"), scm)
        from patchline.models import Installation

        assert sess.query(Installation).filter_by(github_installation_id=77001).one().connection_state == "removed"


class TestInstallationRepositories:
    def test_added_repos_autoscan(self, db, sess, github_fixtures_dir):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor, status="ready")
        sess.commit()
        scm = FixtureGitHubClient(fixture_dir=github_fixtures_dir)
        process_webhook("del-4", "installation", _installation_payload(), scm)
        payload = {
            "action": "added",
            "installation": {"id": 77001, "account": {"login": "acme", "type": "Organization"}},
            "repositories_added": [{"full_name": "acme/new-repo", "default_branch": "main"}],
            "repositories_removed": [],
        }
        process_webhook("del-5", "installation_repositories", payload, scm)
        from patchline.models import Repository, ScanJob

        repo = sess.query(Repository).filter_by(full_name="acme/new-repo").one()
        jobs = sess.query(ScanJob).filter_by(entry_id=entry.id, repository_id=repo.id).all()
        assert jobs and jobs[0].priority == 10


class TestPullRequestWebhook:
    def _open_pr(self, sess, entry_status="ready"):
        from patchline.models import PullRequest

        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor, status=entry_status)
        repo = make_repo_for_pr(sess)
        pr = PullRequest(remediation_id=1, repository_id=repo.id, entry_id=entry.id, pr_number=42, state="open")
        sess.add(pr)
        from patchline.core.coverage import apply_transition

        for target in ("scanning", "pattern_detected", "remediation_pending", "pr_open"):
            apply_transition(sess, entry_id=entry.id, repository_id=repo.id, to_status=target)
        sess.commit()
        return entry, repo, pr

    def test_closed_merged_enqueues_verification(self, db, sess, github_fixtures_dir):
        """FR-024: merged → single verification scan with 5s propagation delay."""
        entry, repo, pr = self._open_pr(sess)
        scm = FixtureGitHubClient(fixture_dir=github_fixtures_dir)
        payload = {
            "action": "closed",
            "pull_request": {"number": 42, "merged": True},
            "repository": {"full_name": repo.full_name},
        }
        process_webhook("del-6", "pull_request", payload, scm)
        from patchline.models import CoverageState, ScanJob

        sess.expire(pr)
        assert pr.state == "merged"
        job = (
            sess.query(ScanJob)
            .filter_by(entry_id=entry.id, repository_id=repo.id)
            .order_by(ScanJob.id.desc())
            .first()
        )
        assert job is not None and job.purpose == "verification"
        assert job.next_attempt_at is not None  # 5s propagation delay
        cov = sess.query(CoverageState).filter_by(entry_id=entry.id, repository_id=repo.id).one()
        assert cov.status == "pr_open"  # until verification lands

    def test_closed_unmerged_rejected(self, db, sess, github_fixtures_dir):
        """FR-036/AC-020: closed without merge → pr_rejected (never migrated)."""
        entry, repo, pr = self._open_pr(sess)
        scm = FixtureGitHubClient(fixture_dir=github_fixtures_dir)
        payload = {
            "action": "closed",
            "pull_request": {"number": 42, "merged": False},
            "repository": {"full_name": repo.full_name},
        }
        process_webhook("del-7", "pull_request", payload, scm)
        from patchline.models import CoverageState

        sess.expire(pr)
        assert pr.state == "rejected"
        cov = sess.query(CoverageState).filter_by(entry_id=entry.id, repository_id=repo.id).one()
        assert cov.status == "pr_rejected"


def make_repo_for_pr(sess):
    from patchline.models import Installation, Repository

    inst = Installation(github_installation_id=77001, account_login="acme", account_type="Organization", connection_state="active")
    sess.add(inst)
    sess.flush()
    repo = Repository(installation_id=inst.id, full_name="acme/widgets", default_branch="main", primary_language="python", connection_state="active")
    sess.add(repo)
    sess.flush()
    return repo
