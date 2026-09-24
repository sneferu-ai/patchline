"""FR-046 canonical diff hashing.

Canonical representation:
- keep only unified-diff content lines: ``--- ``, ``+++ ``, ``@@``, `` ``,
  ``+``, ``-`` (anything else — engine metadata, comments, index lines — is
  dropped);
- on ``---``/``+++`` header lines, drop any tab-separated suffix (timestamps,
  engine run ids) defensively — the adapter is responsible for stripping, the
  canonicalizer is belt-and-suspenders;
- sort files alphabetically by the ``+++`` path;
- preserve the ORIGINAL hunk order within each file (sorting hunks can change
  the semantic meaning of overlapping patches);
- SHA-256 hex of the canonical text. The empty canonical diff hashes to the
  SHA-256 of the empty string (valid, collision-acceptable — but FR-016
  rejects empty diffs as engine errors before hashing).
"""
from __future__ import annotations

import hashlib
from typing import List, Tuple


def _is_header(line: str, marker: str) -> bool:
    return line.startswith(marker + " ") or line == marker


def _header_path(line: str, marker: str) -> str:
    rest = line[len(marker):].strip()
    rest = rest.split("\t", 1)[0].split(" ", 1)[0]
    if rest.startswith("a/") or rest.startswith("b/"):
        rest = rest[2:]
    return rest


def _clean_header(line: str, marker: str) -> str:
    path = _header_path(line, marker)
    prefix = "a/" if marker == "---" else "b/"
    return f"{marker} {prefix}{path}"


def _keep_line(line: str) -> bool:
    if _is_header(line, "---") or _is_header(line, "+++"):
        return True
    if line.startswith("@@"):
        return True
    if line.startswith((" ", "+", "-")):
        # '+++'/'---' already handled above; single-char prefixes are content.
        return True
    return False


def split_file_sections(diff_text: str) -> List[Tuple[str, List[str]]]:
    """Split a unified diff into (path, lines) sections keyed by +++ path."""
    sections: List[Tuple[str, List[str]]] = []
    current_path = None
    current_lines: List[str] = []
    pending_old = None
    for raw in diff_text.splitlines():
        line = raw.rstrip("\n")
        if _is_header(line, "---"):
            if current_path is not None or pending_old is not None:
                if pending_old is not None:
                    current_lines.append(pending_old)
                    pending_old = None
                if current_lines:
                    sections.append((current_path or "", current_lines))
            current_path = None
            current_lines = []
            pending_old = line
            continue
        if _is_header(line, "+++"):
            current_path = _header_path(line, "+++")
            if pending_old is not None:
                current_lines.append(pending_old)
                pending_old = None
            current_lines.append(line)
            continue
        if current_path is None:
            # Content before any ---/+++ pair: ignore (metadata).
            continue
        if _keep_line(line):
            current_lines.append(line)
    if pending_old is not None:
        current_lines.append(pending_old)
    if current_lines:
        sections.append((current_path or "", current_lines))
    return sections


def canonical_diff(diff_text: str) -> str:
    """Return the canonical representation per FR-046."""
    sections = split_file_sections(diff_text or "")
    cleaned = []
    for path, lines in sections:
        out_lines = []
        for line in lines:
            if _is_header(line, "---"):
                out_lines.append(_clean_header(line, "---"))
            elif _is_header(line, "+++"):
                out_lines.append(_clean_header(line, "+++"))
            else:
                out_lines.append(line)
        cleaned.append((path, out_lines))
    cleaned.sort(key=lambda item: item[0])
    out = []
    for _path, lines in cleaned:
        out.extend(lines)
    return "\n".join(out)


def canonical_diff_sha256(diff_text: str) -> str:
    """SHA-256 of the canonical representation (empty diff → sha256 of "")."""
    return hashlib.sha256(canonical_diff(diff_text).encode("utf-8")).hexdigest()
