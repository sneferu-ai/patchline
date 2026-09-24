"""FR-073 slug algorithm.

lowercase → spaces to hyphens → strip chars not in [a-z0-9-] → collapse
consecutive hyphens → strip leading/trailing hyphens → truncate to 50 chars.
An empty result (title was all special chars) falls back to ``entry-{id}``
via the two-step insert handled by the entry service.
"""
from __future__ import annotations

import re

_NON_SLUG_CHARS = re.compile(r"[^a-z0-9-]")
_MULTI_HYPHEN = re.compile(r"-{2,}")


def slugify_title(title: str, max_len: int = 50) -> str:
    """Return the FR-073 slug for a title; may return "" (caller falls back)."""
    slug = (title or "").strip().lower().replace(" ", "-")
    slug = _NON_SLUG_CHARS.sub("", slug)
    slug = _MULTI_HYPHEN.sub("-", slug)
    slug = slug.strip("-")
    return slug[:max_len]


def slug_or_fallback(title: str, entry_id: int) -> str:
    """FR-073: empty slug → ``entry-{id}`` (two-step insert supplies the id)."""
    return slugify_title(title) or f"entry-{entry_id}"
