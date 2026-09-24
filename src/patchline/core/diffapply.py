"""Reconstruct post-patch file content by applying unified diff hunks (FR-017).

The complete post-patch file is rebuilt by applying the diff's hunks to the
original file content (from the scan archive, or re-fetched at the scanned
commit SHA). The result is what the language pack's syntax parser validates.

This is a deliberately small, strict applicator: it verifies context and
removal lines match the original at the stated offsets and raises
:class:`DiffApplyError` on any mismatch — reconstruction failures must be
loud, never silently wrong.
"""
from __future__ import annotations

import re
from typing import List, Tuple

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class DiffApplyError(Exception):
    pass


def parse_hunks(diff_text: str) -> List[Tuple[str, int, List[str]]]:
    """Parse a unified diff into (new_path, old_start, hunk_lines) tuples."""
    hunks: List[Tuple[str, int, List[str]]] = []
    new_path = None
    old_start = None
    body: List[str] = []
    for line in (diff_text or "").splitlines():
        if line.startswith("+++"):
            new_path = line[3:].strip().split("\t", 1)[0]
            if new_path.startswith("b/"):
                new_path = new_path[2:]
            continue
        m = _HUNK_RE.match(line)
        if m:
            if old_start is not None:
                hunks.append((new_path or "", old_start, body))
            old_start = int(m.group(1))
            body = []
            continue
        if old_start is not None:
            if line.startswith((" ", "+", "-")) or line == "":
                body.append(line)
            elif line.startswith("\\"):
                # "\ No newline at end of file" — ignore for reconstruction.
                continue
            else:
                hunks.append((new_path or "", old_start, body))
                old_start = None
                body = []
    if old_start is not None:
        hunks.append((new_path or "", old_start, body))
    return hunks


def apply_unified_diff(original: str, diff_text: str) -> str:
    """Apply all hunks of a single-file diff to ``original``; return new text.

    Raises DiffApplyError on context/removal mismatch or truncated files.
    """
    orig_lines = original.splitlines(keepends=True)
    hunks = parse_hunks(diff_text)
    if not hunks:
        raise DiffApplyError("no hunks found in diff")

    out: List[str] = []
    cursor = 0  # index into orig_lines
    for _path, old_start, body in hunks:
        start_idx = max(old_start - 1, 0)
        if start_idx < cursor:
            raise DiffApplyError("overlapping hunks are not supported")
        out.extend(orig_lines[cursor:start_idx])
        pos = start_idx
        for line in body:
            tag = line[:1]
            text = line[1:]
            if tag == " ":
                if pos >= len(orig_lines):
                    raise DiffApplyError("context beyond end of file")
                if orig_lines[pos].rstrip("\n") != text.rstrip("\n"):
                    raise DiffApplyError(
                        f"context mismatch at original line {pos + 1}: "
                        f"expected {text.rstrip()!r}, found {orig_lines[pos].rstrip()!r}"
                    )
                out.append(orig_lines[pos])
                pos += 1
            elif tag == "-":
                if pos >= len(orig_lines):
                    raise DiffApplyError("removal beyond end of file")
                if orig_lines[pos].rstrip("\n") != text.rstrip("\n"):
                    raise DiffApplyError(
                        f"removal mismatch at original line {pos + 1}: "
                        f"expected {text.rstrip()!r}, found {orig_lines[pos].rstrip()!r}"
                    )
                pos += 1
            elif tag == "+":
                out.append(text if text.endswith("\n") else text + "\n")
            else:
                raise DiffApplyError(f"malformed hunk line: {line!r}")
        cursor = pos
    out.extend(orig_lines[cursor:])
    return "".join(out)


def files_in_diff(diff_text: str) -> List[str]:
    """Ordered list of +++ paths touched by the diff."""
    seen = []
    for line in (diff_text or "").splitlines():
        if line.startswith("+++"):
            path = line[3:].strip().split("\t", 1)[0]
            if path.startswith("b/"):
                path = path[2:]
            if path not in seen:
                seen.append(path)
    return seen


def per_file_diffs(diff_text: str) -> dict:
    """Split a multi-file diff into {path: single-file diff text}."""
    result = {}
    current = None
    buf = []
    for line in (diff_text or "").splitlines():
        if line.startswith("---"):
            if current is not None and buf:
                result[current] = "\n".join(buf)
            current = None
            buf = [line]
            continue
        if line.startswith("+++"):
            path = line[3:].strip().split("\t", 1)[0]
            if path.startswith("b/"):
                path = path[2:]
            current = path
            buf.append(line)
            continue
        if current is not None:
            buf.append(line)
    if current is not None and buf:
        result[current] = "\n".join(buf)
    return result
