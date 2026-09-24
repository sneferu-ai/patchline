"""Dispatch unit tests: branch naming, reuse decision, compare-hash (FR-021/073)."""
from __future__ import annotations

from datetime import datetime, timezone

from patchline.core.dispatch import _canonical_compare_hash, branch_base_name, choose_branch
from patchline.core.diffhash import canonical_diff_sha256


class _Entry:
    id = 9
    slug = "rename-get-user"
    created_at = datetime(2026, 8, 5, tzinfo=timezone.utc)


class TestBranchNaming:
    def test_format(self):
        name = branch_base_name(_Entry(), "abcdef1234567890")
        assert name == "patchline/rename-get-user-20260805-abcdef12"

    def test_slug_fallback(self):
        class E(_Entry):
            slug = None
            id = 7

        assert branch_base_name(E(), "abcdef1234567890").startswith("patchline/entry-7-")


class _FakeSCM:
    """Minimal seam for choose_branch: refs + compare results."""

    def __init__(self, refs, compares):
        self._refs = refs  # branch -> sha
        self._compares = compares  # branch -> compare payload

    def get_branch_ref(self, full_name, branch, token=None):
        if branch not in self._refs:
            from patchline.adapters.scm.github import GitHubError

            raise GitHubError("not found", status=404)
        return {"object": {"sha": self._refs[branch]}}

    def compare(self, full_name, base, head, token=None):
        return self._compares[head]


DIFF = "--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
HASH = canonical_diff_sha256(DIFF)


def _compare_payload(filename="f.py", patch="@@ -1,1 +1,1 @@\n-old\n+new\n"):
    return {"files": [{"filename": filename, "patch": patch}]}


class TestChooseBranch:
    def test_fresh_branch_when_none_exists(self):
        scm = _FakeSCM({}, {})
        name, reuse = choose_branch(scm, "acme/w", "main", "patchline/x-20260805-abcdef12", HASH, {}, None)
        assert name == "patchline/x-20260805-abcdef12"
        assert reuse is False

    def test_reuse_when_hash_matches_and_no_live_pr(self):
        base = "patchline/x-20260805-abcdef12"
        scm = _FakeSCM({base: "sha"}, {base: _compare_payload()})
        name, reuse = choose_branch(scm, "acme/w", "main", base, HASH, {}, None)
        assert name == base
        assert reuse is True

    def test_no_reuse_when_live_pr(self):
        base = "patchline/x-20260805-abcdef12"
        scm = _FakeSCM({base: "sha"}, {base: _compare_payload()})
        name, reuse = choose_branch(scm, "acme/w", "main", base, HASH, {base: True}, None)
        assert name == base + "-2"
        assert reuse is False

    def test_no_reuse_when_hash_differs(self):
        base = "patchline/x-20260805-abcdef12"
        other = _compare_payload(patch="@@ -1,1 +1,1 @@\n-old\n+DIFFERENT\n")
        scm = _FakeSCM({base: "sha"}, {base: other})
        name, reuse = choose_branch(scm, "acme/w", "main", base, HASH, {}, None)
        assert name == base + "-2"
        assert reuse is False

    def test_dispatch_failed_pr_allows_reuse(self):
        base = "patchline/x-20260805-abcdef12"
        scm = _FakeSCM({base: "sha"}, {base: _compare_payload()})
        # existing_branches False = only dispatch_failed PR rows exist.
        name, reuse = choose_branch(scm, "acme/w", "main", base, HASH, {base: False}, None)
        assert reuse is True


class TestCompareHash:
    def test_canonical_compare_hash_matches(self):
        payload = _compare_payload()
        assert _canonical_compare_hash(payload) == HASH

    def test_unreconstructable_returns_none(self):
        assert _canonical_compare_hash({"files": []}) is None
        assert _canonical_compare_hash({"files": [{"filename": "f.py"}]}) is None
