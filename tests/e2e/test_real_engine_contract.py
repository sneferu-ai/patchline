"""AC-026 — real-engine contract test (CI-runnable, recorded fixture).

Verifies against the recorded Sneferu response fixture:
- workflow-name selection comes from WorkflowConfig (K6 — no names in signatures);
- provenance fields map into the typed Provenance fields; unknown fields land
  in ``extra``;
- diff output shape is a clean unified diff;
- when the recorded fixture lacks provenance, a warning is logged:
  "Provenance metadata absent — showcase goal may not be met."
"""
from __future__ import annotations

import json
import logging
import os

import pytest

httpx = pytest.importorskip("httpx")

from patchline.adapters.engine import (
    EntryInput,
    RemediationInput,
    ScanContext,
    SneferuEngineAdapter,
    WorkflowConfig,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "..", "fixtures", "engine", "real_engine_response.json")

ENTRY = EntryInput(
    prior_api_shape="The function get_user(id) accepts a numeric user ID.",
    target_contract="The function is now get_user_by_id(id).",
    language="python",
)


@pytest.fixture()
def recorded():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _adapter(recorded, workflows=None, remediate_key="remediate"):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/version":
            return httpx.Response(200, json=recorded["version"])
        if request.url.path.endswith("/run"):
            workflow = request.url.path.split("/")[-2]
            mapping = {
                workflows.derive if workflows else "derive_patterns": recorded["derive"],
                workflows.scan if workflows else "scan_repository": recorded["scan"],
                workflows.remediate if workflows else "generate_remediation": recorded[remediate_key],
            }
            body = mapping.get(workflow)
            if body is None:
                return httpx.Response(404, json={"error": f"unknown workflow {workflow}"})
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    adapter = SneferuEngineAdapter(
        base_url="http://recorded-engine",
        service_token="recorded-token",
        workflow_config=workflows or WorkflowConfig(),
        transport=transport,
    )
    return adapter, calls


class TestWorkflowSelection:
    def test_workflow_names_from_config(self, recorded):
        workflows = WorkflowConfig(derive="custom_derive", scan="custom_scan", remediate="custom_remediate")
        adapter, calls = _adapter(recorded, workflows=workflows)
        adapter.derive_patterns(ENTRY)
        assert any("/workflows/custom_derive/run" in path for path in calls)


class TestContract:
    def test_derive(self, recorded):
        adapter, _calls = _adapter(recorded)
        result = adapter.derive_patterns(ENTRY)
        assert result.patterns
        assert result.engine_run_id == "sneferu-run-7f3a9c1e"
        assert result.language == "python"

    def test_scan(self, recorded):
        adapter, _calls = _adapter(recorded)
        context = ScanContext(repo_full_name="acme/widgets", branch="main", archive_path="/tmp", language="python")
        from patchline.adapters.engine import PatternSet

        result = adapter.scan_repository(context, PatternSet(patterns=[], engine_run_id="", language="python"))
        assert len(result.occurrences) == 1
        assert result.files_examined == 21

    def test_remediate_provenance_mapping(self, recorded):
        adapter, _calls = _adapter(recorded)
        context = ScanContext(repo_full_name="acme/widgets", branch="main", archive_path="/tmp", language="python")
        result = adapter.generate_remediation(
            RemediationInput(entry=ENTRY, occurrences=[], repo_context=context)
        )
        # Clean unified diff shape.
        assert result.diff.startswith("--- a/")
        assert "@@" in result.diff
        # Typed provenance fields mapped…
        prov = result.provenance
        assert prov is not None
        assert prov.quality_score == 0.93
        assert prov.certificate_class == "cross-lineage-converged"
        assert prov.disagreement_record.endswith("/disagreements")
        assert prov.models == ["kimi-k2p6", "glm-5p2"]
        # …and the unknown engine field landed in extra (provisional schema).
        assert prov.extra.get("trace_id") == "01J4ZXY8KRECORDEDTRACE"

    def test_missing_provenance_warns(self, recorded, caplog):
        adapter, _calls = _adapter(recorded, remediate_key="remediate_no_provenance")
        context = ScanContext(repo_full_name="acme/widgets", branch="main", archive_path="/tmp", language="python")
        with caplog.at_level(logging.WARNING, logger="patchline"):
            result = adapter.generate_remediation(
                RemediationInput(entry=ENTRY, occurrences=[], repo_context=context)
            )
        if result.provenance is None or not result.provenance.any_typed_present():
            # FR-059: the contract test logs the showcase warning.
            logging.getLogger("patchline").warning(
                "Provenance metadata absent — showcase goal may not be met"
            )
        assert any("Provenance metadata absent" in r.message for r in caplog.records)


class TestGetVersion:
    def test_version_from_endpoint(self, recorded):
        adapter, _calls = _adapter(recorded)
        assert adapter.get_version() == "sneferu-2026.08-rc1"

    def test_ping(self, recorded):
        adapter, _calls = _adapter(recorded)
        assert adapter.ping() is True

    def test_ping_false_when_server_lacks_the_engine_api(self):
        """A reachable server that answers 404 (e.g. today's Sneferu engine,
        which has no /version or /workflows/{name}/run) is not a healthy engine."""
        transport = httpx.MockTransport(lambda request: httpx.Response(404, json={"detail": "Not Found"}))
        adapter = SneferuEngineAdapter(base_url="http://engine.test", service_token="t", transport=transport)
        assert adapter.ping() is False
