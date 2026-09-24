"""FR-046 canonical diff hashing tests."""
from __future__ import annotations

import hashlib

from patchline.core.diffhash import canonical_diff, canonical_diff_sha256


DIFF_A = """--- a/src/zeta.py
+++ b/src/zeta.py
@@ -1,1 +1,1 @@
-old zeta
+new zeta
"""

DIFF_B = """--- a/src/alpha.py
+++ b/src/alpha.py
@@ -1,1 +1,1 @@
-old alpha
+new alpha
"""


class TestCanonicalDiff:
    def test_files_sorted_alphabetically(self):
        combined = DIFF_A + DIFF_B
        canon = canonical_diff(combined)
        assert canon.index("src/alpha.py") < canon.index("src/zeta.py")

    def test_order_independent_hash(self):
        assert canonical_diff_sha256(DIFF_A + DIFF_B) == canonical_diff_sha256(DIFF_B + DIFF_A)

    def test_metadata_lines_dropped(self):
        with_meta = "index 123..456 100644\n" + DIFF_A
        assert "index" not in canonical_diff(with_meta)

    def test_header_timestamps_stripped(self):
        with_ts = "--- a/src/zeta.py\t2026-08-05 10:00:00\n+++ b/src/zeta.py\t2026-08-05 10:00:01\n@@ -1,1 +1,1 @@\n-old zeta\n+new zeta\n"
        assert canonical_diff(with_ts) == canonical_diff(DIFF_A)

    def test_hunk_order_preserved_within_file(self):
        diff = (
            "--- a/src/f.py\n+++ b/src/f.py\n"
            "@@ -10,1 +10,1 @@\n-a\n+A\n"
            "@@ -2,1 +2,1 @@\n-b\n+B\n"
        )
        canon = canonical_diff(diff)
        assert canon.index("@@ -10,") < canon.index("@@ -2,")

    def test_empty_diff_hashes_empty_string(self):
        assert canonical_diff_sha256("") == hashlib.sha256(b"").hexdigest()
        assert canonical_diff_sha256("garbage\nnot a diff\n") == hashlib.sha256(b"").hexdigest()

    def test_ab_prefixes_normalized(self):
        diff = "--- src/f.py\n+++ src/f.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        canon = canonical_diff(diff)
        assert "--- a/src/f.py" in canon
        assert "+++ b/src/f.py" in canon
