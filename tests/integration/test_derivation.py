"""Derivation flow tests (FR-010/FR-039/FR-041)."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("sqlalchemy")

from patchline.adapters.engine import EngineError, MockEngineAdapter
from patchline.core import derivation
from patchline.core.derivation import derive_entry, feedback_for_language, process_pending_derivations

from conftest import make_entry, make_vendor


class _FailingEngine:
    def derive_patterns(self, entry, feedback=None):
        raise EngineError("engine unreachable")

    def get_version(self):
        return "failing-0"


class TestDerive:
    def test_draft_to_ready(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor, status="draft")
        sess.commit()
        result = derive_entry(entry.id, MockEngineAdapter(fixture_dir="/nonexistent"))
        assert result == "ready"
        sess.expire(entry)
        assert entry.status == "ready"
        from patchline.models import DerivedPattern

        pattern = sess.query(DerivedPattern).filter_by(entry_id=entry.id).one()
        assert pattern.engine_run_id
        assert json.loads(pattern.pattern_json)

    def test_engine_error_to_failed(self, sess):
        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor, status="draft")
        sess.commit()
        result = derive_entry(entry.id, _FailingEngine())
        assert result == "derivation_failed"
        sess.expire(entry)
        assert entry.status == "derivation_failed"

    def test_process_pending(self, sess):
        vendor = make_vendor(sess)
        e1 = make_entry(sess, vendor, title="one", status="draft")
        e2 = make_entry(sess, vendor, title="two", status="draft")
        sess.commit()
        processed = process_pending_derivations(MockEngineAdapter(fixture_dir="/nonexistent"))
        assert processed == 2
        sess.expire_all()
        assert e1.status == "ready"
        assert e2.status == "ready"

    def test_config_failure_count_reset_on_success(self, sess):
        from patchline.core import config_store

        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor, status="draft")
        sess.commit()
        derive_entry(entry.id, _FailingEngine())
        assert config_store.engine_consecutive_failures(sess) == 1
        entry2 = make_entry(sess, vendor, title="second", status="draft")
        sess.commit()
        derive_entry(entry2.id, MockEngineAdapter(fixture_dir="/nonexistent"))
        assert config_store.engine_consecutive_failures(sess) == 0


class TestFeedback:
    def test_feedback_gathered_by_language(self, sess):
        from patchline.models import FeedbackFlag, Occurrence, ScanJob

        vendor = make_vendor(sess)
        entry = make_entry(sess, vendor, language="python")
        job = ScanJob(entry_id=entry.id, repository_id=1, branch="main", status="done")
        sess.add(job)
        sess.flush()
        occ = Occurrence(scan_job_id=job.id, file_path="a.py", line_start=1, line_end=1, snippet="old_call()")
        sess.add(occ)
        sess.flush()
        sess.add(FeedbackFlag(occurrence_id=occ.id, vendor_id=vendor.id, reason="not real"))
        sess.flush()
        signals = feedback_for_language(sess, "python")
        assert len(signals) == 1
        assert signals[0].occurrence_snippet == "old_call()"
        assert signals[0].reason == "not real"
        # Other languages see nothing.
        assert feedback_for_language(sess, "go") == []
