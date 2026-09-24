"""Remediation generation flow (FR-016/FR-017/FR-046).

Pipeline: fetch archive at the scanned commit SHA (the scan archive is long
deleted) → engine ``generate_remediation`` → ``unidiff`` structural
validation (malformed → fast fail) → reconstruct each post-patch file and run
the language pack's syntax parser (error carries line/column/text) →
canonical diff hash → store. Any failure marks the remediation
``generation_failed`` with the reason; nothing is silently repaired.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

from ..adapters.engine import (
    EngineError,
    EntryInput,
    OccurrenceData,
    RemediationInput,
    ScanContext,
)
from ..adapters.languages import get_pack
from ..db import session_scope
from .coverage import apply_transition
from .diffapply import DiffApplyError, apply_unified_diff, files_in_diff, per_file_diffs
from .diffhash import canonical_diff_sha256
from .scanning import fetch_archive_dir, repo_installation_id

log = logging.getLogger("patchline.remediation")

MALFORMED_DIFF_MESSAGE = "engine returned malformed diff"
EMPTY_DIFF_MESSAGE = "engine returned empty diff"


class RemediationFailure(Exception):
    pass


def validate_diff_structure(diff_text: str) -> Optional[str]:
    """FR-017: ``unidiff`` structural validation.

    Returns an error message when malformed, else None. Missing @@ headers,
    wrong line counts, or context-line mismatches fail fast here rather than
    surfacing as a confusing syntax error later.
    """
    if not diff_text or not diff_text.strip():
        return EMPTY_DIFF_MESSAGE
    if "@@" not in diff_text or "+++" not in diff_text:
        return MALFORMED_DIFF_MESSAGE
    try:
        import unidiff
    except ImportError:
        # Dev-sandbox fallback: the spec mandates unidiff; without it the
        # presence checks above are the floor. Noted in IMPLEMENTATION_NOTES.
        log.warning("unidiff not installed; structural diff validation limited to header presence")
        return None
    try:
        patch_set = unidiff.PatchSet(diff_text.splitlines(keepends=True))
    except Exception as exc:  # noqa: BLE001 - unidiff raises several types
        return f"{MALFORMED_DIFF_MESSAGE}: {exc}"
    if not list(patch_set):
        return MALFORMED_DIFF_MESSAGE
    for patched_file in patch_set:
        if not list(patched_file):
            return f"{MALFORMED_DIFF_MESSAGE}: file {patched_file.path!r} has no hunks"
    return None


def reconstruct_patched_file(original: str, file_diff: str) -> str:
    """Apply the diff hunks to the original content (FR-017)."""
    return apply_unified_diff(original, file_diff)


def syntax_validate_remediation(diff_text: str, archive_dir: str, language: str) -> Optional[str]:
    """Reconstruct each modified file and run the pack's syntax parser.

    Returns an error string (with line/column/text when available) or None.
    Only files matching the language pack's extensions are validated.
    """
    pack = get_pack(language)
    if pack is None:
        return None  # no pack → nothing to validate against (should not happen; language validated at entry)
    extensions = tuple(pack.file_extensions())
    for path, file_diff in per_file_diffs(diff_text).items():
        if not path.endswith(extensions):
            continue
        original_path = os.path.join(archive_dir, path)
        try:
            with open(original_path, "r", encoding="utf-8") as fh:
                original = fh.read()
        except OSError:
            original = ""  # new file introduced by the diff
        try:
            patched = reconstruct_patched_file(original, file_diff)
        except DiffApplyError as exc:
            return f"could not reconstruct {path}: {exc}"
        result = pack.parse_syntax(patched)
        if not result.valid:
            location = ""
            if result.lineno is not None:
                location = f" (line {result.lineno}"
                if result.offset is not None:
                    location += f", column {result.offset}"
                location += ")"
            detail = result.error or "syntax error"
            if result.text:
                detail += f" — offending text: {result.text.strip()[:200]}"
            return f"{path}{location}: {detail}"
    return None


def generate_remediation(remediation_id: int, engine, scm, work_parent: Optional[str] = None) -> str:
    """Worker entrypoint: execute a 'generating' Remediation row end to end."""
    from ..models import ChangelogEntry, Occurrence, Remediation, Repository, ScanJob

    archive_dir: Optional[str] = None
    with session_scope() as sess:
        rem = sess.get(Remediation, remediation_id)
        if rem is None:
            return "missing"
        entry = sess.get(ChangelogEntry, rem.entry_id)
        repo = sess.get(Repository, rem.repository_id)
        if entry is None or repo is None:
            rem.status = "generation_failed"
            rem.error_message = "entry or repository missing"
            return "generation_failed"
        scan_job = sess.get(ScanJob, rem.scan_job_id) if rem.scan_job_id else None
        occurrences = [
            OccurrenceData(
                file_path=o.file_path,
                line_start=o.line_start,
                line_end=o.line_end,
                snippet=o.snippet or "",
                confidence=o.confidence,
            )
            for o in sess.query(Occurrence).filter_by(scan_job_id=rem.scan_job_id).all()
        ] if rem.scan_job_id else []
        entry_input = EntryInput(
            prior_api_shape=entry.prior_api_shape,
            target_contract=entry.target_contract,
            language=entry.language,
        )
        language = entry.language
        entry_id = entry.id
        repo_id = repo.id
        repo_full_name = repo.full_name
        branch = repo.default_branch
        scanned_sha = scan_job.scanned_sha if scan_job else None

    if not occurrences:
        with session_scope() as sess:
            rem = sess.get(Remediation, remediation_id)
            rem.status = "generation_failed"
            rem.error_message = "no default-branch occurrences to remediate"
        return "generation_failed"

    try:
        installation_id = repo_installation_id(repo_id)
        token = scm.mint_installation_token(installation_id) if installation_id else None
        # FR-016: the scan archive is deleted within 10 minutes; re-fetch at
        # the scanned commit SHA before calling the engine.
        ref = scanned_sha or branch
        archive_dir, _sha = fetch_archive_dir_for_repo(scm, repo_full_name, branch, ref, token, work_parent)
        context = ScanContext(
            repo_full_name=repo_full_name,
            branch=branch,
            archive_path=archive_dir,
            language=language,
        )
        result = engine.generate_remediation(
            RemediationInput(entry=entry_input, occurrences=occurrences, repo_context=context)
        )
    except EngineError as exc:
        with session_scope() as sess:
            rem = sess.get(Remediation, remediation_id)
            rem.status = "generation_failed"
            rem.error_message = f"engine error: {exc}"
            from .scanning import _notify_engine_failure

            _notify_engine_failure(sess, entry_id, repo_full_name)
        from . import notifications as _notifications

        _notifications.flush_pending()  # deliver AFTER the tx commits (CP-2)
        return "generation_failed"
    except Exception as exc:  # noqa: BLE001
        log.exception("remediation %s crashed", remediation_id)
        with session_scope() as sess:
            rem = sess.get(Remediation, remediation_id)
            rem.status = "generation_failed"
            rem.error_message = f"unexpected: {exc}"
        return "generation_failed"

    try:
        structural_error = validate_diff_structure(result.diff)
        if structural_error:
            raise RemediationFailure(structural_error)
        syntax_error = syntax_validate_remediation(result.diff, archive_dir, language)
        if syntax_error:
            raise RemediationFailure(f"syntax validation failed: {syntax_error}")
        diff_hash = canonical_diff_sha256(result.diff)
    except RemediationFailure as exc:
        with session_scope() as sess:
            rem = sess.get(Remediation, remediation_id)
            rem.status = "generation_failed"
            rem.error_message = str(exc)
        return "generation_failed"
    finally:
        if archive_dir:
            shutil.rmtree(archive_dir, ignore_errors=True)

    with session_scope() as sess:
        from .config_store import record_engine_success
        import json

        rem = sess.get(Remediation, remediation_id)
        rem.diff_content = result.diff
        rem.diff_sha256 = diff_hash
        rem.engine_run_id = result.engine_run_id
        rem.provenance_json = json.dumps(result.provenance.to_json_dict()) if result.provenance else None
        rem.status = "ready"
        rem.error_message = None
        rem.updated_at = datetime.now(timezone.utc)
        record_engine_success(sess)
        apply_transition(
            sess,
            entry_id=entry_id,
            repository_id=repo_id,
            to_status="remediation_pending",
            job_type="remediation",
        )
    return "ready"


def fetch_archive_dir_for_repo(scm, repo_full_name: str, branch: str, ref: str, token: Optional[str], work_parent: Optional[str]):
    """fetch_archive_dir variant keyed by names (used when only the sha is known)."""
    class _RepoShim:
        full_name = repo_full_name
        default_branch = branch

    from .scanning import fetch_archive_dir

    return fetch_archive_dir(scm, _RepoShim(), ref, token, work_parent=work_parent)


def enqueue_remediation(sess, entry_id: int, repository_id: int, scan_job_id: Optional[int]):
    """FR-016: create a new Remediation row (status 'generating'); any prior
    confirmation is now stale (FR-020) because a new row exists."""
    from ..models import Remediation

    rem = Remediation(
        entry_id=entry_id,
        repository_id=repository_id,
        scan_job_id=scan_job_id,
        status="generating",
    )
    sess.add(rem)
    sess.flush()
    return rem
