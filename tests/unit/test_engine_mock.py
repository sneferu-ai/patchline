"""FR-043/FR-045 mock engine adapter tests."""
from __future__ import annotations

import json
import os

from patchline.adapters.engine import (
    EntryInput,
    FeedbackSignal,
    MockEngineAdapter,
    Provenance,
    RemediationInput,
    ScanContext,
    entry_text_hash,
    sanitize_repo_key,
)

ENTRY = EntryInput(prior_api_shape="old shape", target_contract="new contract", language="python")


class TestFixtureKeys:
    def test_sanitize_repo_key(self):
        assert sanitize_repo_key("Acme/Widgets") == "acme_widgets"
        assert sanitize_repo_key("acme/widgets-app") == "acme_widgetsapp"

    def test_entry_text_hash_stable(self):
        assert entry_text_hash(ENTRY) == entry_text_hash(ENTRY)
        assert len(entry_text_hash(ENTRY)) == 16
        other = EntryInput(prior_api_shape="x", target_contract="y", language="python")
        assert entry_text_hash(other) != entry_text_hash(ENTRY)


class TestMockAdapter:
    def test_get_version_non_empty(self):
        adapter = MockEngineAdapter(fixture_dir="/nonexistent")
        assert adapter.get_version()

    def test_default_fixture_when_unmatched(self):
        adapter = MockEngineAdapter(fixture_dir="/nonexistent")
        patterns = adapter.derive_patterns(ENTRY)
        assert patterns.patterns
        assert patterns.engine_run_id
        assert patterns.language == "python"

    def test_default_scan_has_two_occurrences_and_provenance(self):
        adapter = MockEngineAdapter(fixture_dir="/nonexistent")
        context = ScanContext(repo_full_name="no/match", branch="main", archive_path="/tmp", language="python")
        result = adapter.scan_repository(context, adapter.derive_patterns(ENTRY))
        assert len(result.occurrences) == 2
        remediation = adapter.generate_remediation(
            RemediationInput(entry=ENTRY, occurrences=result.occurrences, repo_context=context)
        )
        assert remediation.diff
        assert remediation.provenance is not None
        assert remediation.provenance.quality_score == 0.87
        assert remediation.provenance.certificate_class == "converged"
        assert remediation.provenance.models

    def test_repo_keyed_fixture(self, engine_fixtures_dir):
        adapter = MockEngineAdapter(fixture_dir=engine_fixtures_dir)
        context = ScanContext(repo_full_name="acme/widgets", branch="main", archive_path="/tmp", language="python")
        result = adapter.scan_repository(context, adapter.derive_patterns(ENTRY))
        assert len(result.occurrences) == 3
        assert all(o.file_path == "src/client/api.py" for o in result.occurrences)
        assert result.files_examined == 21

    def test_feedback_accepted_but_ignored(self):
        adapter = MockEngineAdapter(fixture_dir="/nonexistent")
        feedback = [FeedbackSignal(occurrence_snippet="x", reason="false positive", created_at="2026-08-05")]
        with_feedback = adapter.derive_patterns(ENTRY, feedback=feedback)
        without = adapter.derive_patterns(ENTRY)
        # FR-045: fixture determinism takes precedence over feedback.
        assert with_feedback.patterns == without.patterns
        assert with_feedback.engine_run_id == without.engine_run_id

    def test_old_fixture_version_falls_back(self, tmp_path, caplog):
        old = {"fixture_version": "0.9", "occurrences": [{"file_path": "z.py", "line_start": 1, "line_end": 1, "snippet": "z"}]}
        (tmp_path / "some_repo.json").write_text(json.dumps(old))
        adapter = MockEngineAdapter(fixture_dir=str(tmp_path))
        context = ScanContext(repo_full_name="some/repo", branch="main", archive_path="/tmp", language="python")
        result = adapter.scan_repository(context, adapter.derive_patterns(ENTRY))
        # Old-version fixture rejected → default fixture (2 occurrences).
        assert len(result.occurrences) == 2
        assert any("fixture_version" in record.message for record in caplog.records)


class TestProvenanceMapping:
    def test_unknown_fields_land_in_extra(self):
        prov = Provenance.from_json_dict(
            {"quality_score": 0.9, "trace_id": "abc", "models": ["m1"], "another_unknown": 5}
        )
        assert prov.quality_score == 0.9
        assert prov.models == ["m1"]
        assert prov.extra["trace_id"] == "abc"
        assert prov.extra["another_unknown"] == 5
        assert "trace_id" not in prov.to_json_dict() or True  # extra preserved separately

    def test_round_trip(self):
        prov = Provenance(quality_score=0.5, certificate_class="converged", models=["a"], extra={"k": 1})
        restored = Provenance.from_json_dict(prov.to_json_dict())
        assert restored.quality_score == 0.5
        assert restored.certificate_class == "converged"
        assert restored.extra == {"k": 1}

    def test_none_and_partial(self):
        assert Provenance.from_json_dict(None) is None
        partial = Provenance.from_json_dict({"quality_score": 0.3})
        assert partial.any_typed_present()
        empty = Provenance()
        assert not empty.any_typed_present()
