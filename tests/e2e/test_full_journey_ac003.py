"""AC-003 — complete primary journey with the mock engine and fixture GitHub
client (hermetic; no web server, no network).

Journey: entry → derivation → scan → occurrences → remediation (hash verified
independently) → confirmation → dispatch (Git Data call log asserted) → PR row
→ pr_open → verification scan → migrated.

The test independently computes the SHA-256 of the canonical diff from the
FIXTURE text and asserts it matches the stored remediation hash.
"""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("sqlalchemy")

from patchline.adapters.engine import MockEngineAdapter
from patchline.adapters.scm.mock_github import FixtureGitHubClient
from patchline.core import derivation, dispatch, jobs
from patchline.core.diffhash import canonical_diff_sha256
from patchline.core.remediation_flow import generate_remediation
from patchline.core.scanning import execute_scan_job

from conftest import make_entry, make_repo, make_vendor


@pytest.fixture()
def journey(db, sess, engine_fixtures_dir, github_fixtures_dir, fixtures_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(tmp_path / ".hb"))
    engine = MockEngineAdapter(fixture_dir=engine_fixtures_dir)
    scm = FixtureGitHubClient(fixture_dir=github_fixtures_dir)
    with open(os.path.join(fixtures_dir, "entry.json"), "r", encoding="utf-8") as fh:
        entry_text = json.load(fh)
    return {
        "engine": engine,
        "scm": scm,
        "entry_text": entry_text,
        "sess": sess,
    }


class TestFullJourney:
    def test_journey(self, journey, sess):
        engine, scm, entry_text = journey["engine"], journey["scm"], journey["entry_text"]

        # --- author + derive --------------------------------------------------
        vendor = make_vendor(sess)
        from patchline.models import ChangelogEntry

        entry = ChangelogEntry(
            vendor_id=vendor.id,
            title=entry_text["title"],
            prior_api_shape=entry_text["prior_api_shape"],
            target_contract=entry_text["target_contract"],
            language=entry_text["language"],
            status="draft",
        )
        sess.add(entry)
        sess.flush()
        sess.commit()
        assert derivation.derive_entry(entry.id, engine) == "ready"
        from patchline.models import DerivedPattern

        sess.expire_all()
        pattern = sess.query(DerivedPattern).filter_by(entry_id=entry.id).one()
        assert pattern.engine_run_id

        # --- connect repo + scan ----------------------------------------------
        repo = make_repo(sess, full_name="acme/widgets", language="python", default_branch="main")
        sess.commit()
        created = jobs.enqueue_scans_for_entry(sess, entry, [repo], priority=jobs.PRIORITY_USER)
        assert len(created) == 1
        sess.commit()
        job = jobs.claim_next_job(sess)
        assert job is not None
        sess.commit()
        assert execute_scan_job(job.id, engine, scm) == "done"

        from patchline.models import CoverageState, Occurrence, ScanJob

        sess.expire_all()
        job = sess.get(ScanJob, job.id)
        assert job.status == "done"
        assert job.occurrences_count == 3  # acme_widgets engine fixture
        occurrences = sess.query(Occurrence).filter_by(scan_job_id=job.id).all()
        assert len(occurrences) == 3
        cov = sess.query(CoverageState).filter_by(entry_id=entry.id, repository_id=repo.id).one()
        assert cov.status == "pattern_detected"

        # --- remediation --------------------------------------------------------
        from patchline.core.remediation_flow import enqueue_remediation

        rem = enqueue_remediation(sess, entry.id, repo.id, job.id)
        sess.commit()
        assert generate_remediation(rem.id, engine, scm) == "ready"

        from patchline.models import Remediation

        sess.expire_all()
        rem = sess.get(Remediation, rem.id)
        assert rem.status == "ready"
        assert rem.diff_sha256
        # Independent hash: computed from the FIXTURE diff text, not the DB.
        with open(
            os.path.join(journey_fixtures_engine(), "acme_widgets.json"), "r", encoding="utf-8"
        ) as fh:
            fixture_diff = json.load(fh)["diff"]
        expected_hash = canonical_diff_sha256(fixture_diff)
        assert rem.diff_sha256 == expected_hash
        cov = sess.query(CoverageState).filter_by(entry_id=entry.id, repository_id=repo.id).one()
        assert cov.status == "remediation_pending"

        # --- confirmation gate (FR-020): dispatch without confirmation fails ----
        with pytest.raises(dispatch.StaleConfirmation):
            dispatch.dispatch_remediation(rem.id, scm, vendor.id)

        # --- approve + dispatch ---------------------------------------------------
        dispatch.record_confirmation(sess, remediation=rem, vendor_id=vendor.id)
        sess.commit()
        calls_before = len(scm.calls)
        assert dispatch.dispatch_remediation(rem.id, scm, vendor.id) == "dispatched"
        sess.expire_all()
        git_data_paths = [c["path"] for c in scm.calls[calls_before:] if "/git/" in c["path"]]
        assert any(p.endswith("/git/refs") for p in git_data_paths), "branch ref must be created"
        assert any("/git/blobs" in p for p in git_data_paths)
        assert any("/git/trees" in p for p in git_data_paths)
        assert any("/git/commits" in p for p in git_data_paths)
        assert any("/pulls" in p for p in [c["path"] for c in scm.calls[calls_before:]])

        from patchline.models import Confirmation, PullRequest

        conf = sess.query(Confirmation).filter_by(remediation_id=rem.id).one()
        assert conf.diff_sha256 == expected_hash
        pr = sess.query(PullRequest).filter_by(remediation_id=rem.id).one()
        assert pr.pr_number is not None
        assert pr.state == "open"
        assert pr.branch_name.startswith("patchline/")
        cov = sess.query(CoverageState).filter_by(entry_id=entry.id, repository_id=repo.id).one()
        assert cov.status == "pr_open"

        # --- verification scan → migrated -----------------------------------------
        vjob = jobs.enqueue_verification(sess, entry, repo, delay_seconds=0)
        sess.commit()
        # The fixture scan returns 3 occurrences on the default branch, so to
        # simulate a merged-and-fixed repository we point the engine at a repo
        # with no fixture-backed occurrences by overriding the scan result.
        engine_zero = _ZeroOccurrenceEngine(engine)
        assert execute_scan_job(vjob.id, engine_zero, scm) == "done"
        sess.expire_all()
        cov = sess.query(CoverageState).filter_by(entry_id=entry.id, repository_id=repo.id).one()
        assert cov.status == "migrated"
        assert cov.verification_timestamp is not None
        assert cov.manual_migrated is False


class _ZeroOccurrenceEngine:
    """Post-merge engine state: the old pattern is gone."""

    def __init__(self, inner):
        self._inner = inner

    def get_version(self):
        return self._inner.get_version()

    def scan_repository(self, context, patterns):
        from patchline.adapters.engine import ScanResult

        return ScanResult(occurrences=[], files_examined=21, engine_run_id="mock-verify-0")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def journey_fixtures_engine() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "fixtures", "engine")
