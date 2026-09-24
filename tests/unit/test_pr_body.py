"""P12 / FR-054 PR body composition tests."""
from __future__ import annotations

from patchline.adapters.engine import Provenance
from patchline.core.pr_body import MINIMAL_NOTICE, body_has_only_minimal_notice, compose_pr_body


class TestPRBody:
    def test_full_provenance(self):
        prov = Provenance(
            quality_score=0.87,
            certificate_class="converged",
            disagreement_record="https://x/disagreements",
            models=["m1", "m2"],
        )
        body = compose_pr_body(entry_title="Rename get_user", summary="new contract", provenance=prov, scan_link="https://app/scan/1")
        assert "Quality score: 0.87" in body
        assert "Certificate class: converged" in body
        assert "Disagreement record: https://x/disagreements" in body
        assert "Models involved: m1, m2" in body
        assert "https://app/scan/1" in body
        assert MINIMAL_NOTICE in body
        # Trust-guarantee language (spec: displayed in the PR body).
        assert "does not verify" in body or "does not guarantee" in body

    def test_partial_provenance_renders_independently(self):
        prov = Provenance(quality_score=0.5)
        body = compose_pr_body(entry_title="T", summary=None, provenance=prov)
        assert "Quality score: 0.5" in body
        assert "Certificate class" not in body
        assert "Disagreement record" not in body
        # No per-field "not available" placeholders (FR-054).
        assert "not available" not in body.lower()

    def test_no_provenance_minimal_notice_only(self):
        body = compose_pr_body(entry_title="T", summary=None, provenance=None)
        assert MINIMAL_NOTICE in body
        assert body_has_only_minimal_notice(body)
        assert "Provenance" not in body

    def test_empty_provenance_treated_as_absent(self):
        body = compose_pr_body(entry_title="T", summary=None, provenance=Provenance())
        assert "Certificate class" not in body
        assert body_has_only_minimal_notice(body)

    def test_no_sneferu_brand(self):
        prov = Provenance(quality_score=0.9, certificate_class="c", models=["m"])
        body = compose_pr_body(entry_title="T", summary="s", provenance=prov)
        assert "sneferu" not in body.lower()
