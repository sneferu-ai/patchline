"""FR-017 reconstruction tests (apply unified hunks to original content)."""
from __future__ import annotations

import pytest

from patchline.core.diffapply import DiffApplyError, apply_unified_diff, files_in_diff, per_file_diffs

ORIGINAL = "\"\"\"Doc.\"\"\"\n\n\ndef load_user(user_id):\n    user = get_user(user_id)\n    return user\n"

DIFF = (
    "--- a/src/client/api.py\n"
    "+++ b/src/client/api.py\n"
    "@@ -4,3 +4,3 @@\n"
    " def load_user(user_id):\n"
    "-    user = get_user(user_id)\n"
    "+    user = get_user_by_id(user_id)\n"
    "     return user\n"
)


class TestApply:
    def test_reconstruct(self):
        patched = apply_unified_diff(ORIGINAL, DIFF)
        assert "get_user_by_id(user_id)" in patched
        assert "get_user(user_id)" not in patched.replace("get_user_by_id", "")
        assert patched.startswith('"""Doc."""')

    def test_context_mismatch_raises(self):
        bad = ORIGINAL.replace("return user", "return None")
        with pytest.raises(DiffApplyError):
            apply_unified_diff(bad, DIFF)

    def test_removal_mismatch_raises(self):
        diff = DIFF.replace("-    user = get_user(user_id)", "-    something else entirely")
        with pytest.raises(DiffApplyError):
            apply_unified_diff(ORIGINAL, diff)

    def test_new_file_from_empty(self):
        diff = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+line one\n+line two\n"
        assert apply_unified_diff("", diff) == "line one\nline two\n"

    def test_no_hunks_raises(self):
        with pytest.raises(DiffApplyError):
            apply_unified_diff(ORIGINAL, "--- a/x\n+++ b/x\n")

    def test_files_and_split(self):
        multi = DIFF + "--- a/other.py\n+++ b/other.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        assert files_in_diff(multi) == ["src/client/api.py", "other.py"]
        parts = per_file_diffs(multi)
        assert set(parts) == {"src/client/api.py", "other.py"}
        assert "get_user_by_id" in parts["src/client/api.py"]
