"""FR-073 slug algorithm tests."""
from __future__ import annotations

from patchline.core.slugify import slug_or_fallback, slugify_title


class TestSlugify:
    def test_basic(self):
        # FR-073: only [a-z0-9-] survives — underscores are stripped.
        assert slugify_title("Rename get_user to get_user_by_id") == "rename-getuser-to-getuserbyid"

    def test_spaces_to_hyphens(self):
        assert slugify_title("a b  c") == "a-b-c"

    def test_special_chars_removed(self):
        assert slugify_title("API v2.0: Breaking!") == "api-v20-breaking"

    def test_collapse_and_strip_hyphens(self):
        assert slugify_title("--hello---world--") == "hello-world"

    def test_unicode_stripped(self):
        assert slugify_title("Renommer l'API — été") == "renommer-lapi-t"

    def test_truncates_to_50(self):
        slug = slugify_title("x" * 100)
        assert len(slug) <= 50

    def test_empty_when_all_special(self):
        assert slugify_title("!!!***") == ""

    def test_fallback_entry_id(self):
        assert slug_or_fallback("!!!***", 42) == "entry-42"
        assert slug_or_fallback("Normal Title", 42) == "normal-title"
