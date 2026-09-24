"""Scan execution (FR-013/FR-024/FR-055/FR-063/FR-070/FR-072).

Network I/O (token minting, archive download, engine calls) happens OUTSIDE
any database transaction (CP-2); a short write transaction persists results.
Temporary archives are deleted immediately after the job finishes or fails
(well inside the 10-minute bound, §3 persistence boundaries).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Optional, Tuple

from ..adapters.engine import EngineError, ScanContext
from ..adapters.scm.github import ArchiveTooLarge, GitHubError, RateLimitError
from ..db import session_scope
from . import config_store, jobs, notifications
from .coverage import apply_transition

log = logging.getLogger("patchline.scanning")


class ScanFailure(Exception):
    pass


# ---------------------------------------------------------------------------
# Archive acquisition


def _extract_zip(zip_path: str, dest_dir: str) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    entries = [os.path.join(dest_dir, e) for e in os.listdir(dest_dir)]
    dirs = [e for e in entries if os.path.isdir(e)]
    if len(dirs) == 1 and len(entries) == 1:
        return dirs[0]
    return dest_dir


def _dir_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _shallow_clone(full_name: str, branch: str, token: Optional[str], dest_parent: str) -> str:
    """FR-072: shallow clone fallback when the archive exceeds 500MB."""
    if shutil.which("git") is None:
        raise ScanFailure("archive exceeded 500MB and git is not available for the shallow-clone fallback")
    dest = tempfile.mkdtemp(prefix="patchline-clone-", dir=dest_parent)
    if token:
        url = f"https://x-access-token:{token}@github.com/{full_name}.git"
    else:
        url = f"https://github.com/{full_name}.git"
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", "--branch", branch, url, dest],
            check=True,
            capture_output=True,
            timeout=600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise ScanFailure(f"shallow clone fallback failed: {exc}") from exc
    from ..adapters.scm.github import ARCHIVE_MAX_BYTES

    if _dir_size_bytes(dest) > ARCHIVE_MAX_BYTES:
        shutil.rmtree(dest, ignore_errors=True)
        raise ScanFailure("shallow clone fallback also exceeds 500MB")
    return dest


def fetch_archive_dir(scm, repo, ref: str, token: Optional[str], work_parent: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Return (extracted_dir, sha_or_none). Caller deletes the temp tree.

    Mock clients provide ``fetch_archive_dir`` directly; real clients go
    through download → extract, with the shallow-clone fallback (FR-072).
    """
    if hasattr(scm, "fetch_archive_dir"):
        return scm.fetch_archive_dir(repo.full_name, ref, dest_parent=work_parent), None

    ref_obj = scm.get_branch_ref(repo.full_name, ref, token=token)
    sha = (ref_obj.get("object") or {}).get("sha")
    fd, tmp_zip = tempfile.mkstemp(prefix="patchline-archive-", suffix=".zip", dir=work_parent)
    os.close(fd)
    try:
        scm.download_archive(repo.full_name, ref, tmp_zip, token=token)
    except ArchiveTooLarge:
        return _shallow_clone(repo.full_name, ref, token, work_parent), sha
    dest = tempfile.mkdtemp(prefix="patchline-extract-", dir=work_parent)
    try:
        extracted = _extract_zip(tmp_zip, dest)
    finally:
        try:
            os.unlink(tmp_zip)
        except OSError:
            pass
    return extracted, sha


# ---------------------------------------------------------------------------
# Job execution


def _notify_engine_failure(sess, entry_id: Optional[int], repo_name: Optional[str]) -> int:
    """Persist the failure count (survives restarts, FR-027); fire the
    persistent-failure notification at the AC-009 threshold of 3."""
    failures = config_store.record_engine_failure(sess)
    if failures >= 3:
        notifications.record_and_deliver(
            sess,
            "engine_failure",
            instance_id=config_store.instance_id(sess),
            entry_id=entry_id,
            repository_name=repo_name,
            status="unreachable",
            next_attempt_at=notifications.engine_failure_next_attempt_at(failures),
        )
    return failures


def execute_scan_job(job_id: int, engine, scm, work_parent: Optional[str] = None) -> str:
    """Run one claimed scan job end to end. Returns the final job status.

    Coverage transitions honor the job purpose:
    - scan / manual_rescan: scanning → no_hits | pattern_detected
    - verification (FR-024): 0 occurrences → migrated(+timestamp); >0 → migration_incomplete
    - rescan (FR-063 periodic): 0 → keep status; >0 → pattern_detected + regression_detected
    """
    from ..models import ChangelogEntry, DerivedPattern, Occurrence, Repository, ScanJob

    archive_dir: Optional[str] = None
    with session_scope() as sess:
        job = sess.get(ScanJob, job_id)
        if job is None:
            return "missing"
        repo = sess.get(Repository, job.repository_id)
        entry = sess.get(ChangelogEntry, job.entry_id)
        if repo is None or entry is None:
            job.status = "failed"
            job.error_message = "repository or entry missing"
            job.completed_at = datetime.now(timezone.utc)
            return "failed"
        if repo.connection_state != "active":  # FR-042
            job.status = "failed"
            job.error_message = f"repository connection_state={repo.connection_state}"
            job.completed_at = datetime.now(timezone.utc)
            return "failed"
        pattern_row = (
            sess.query(DerivedPattern)
            .filter_by(entry_id=entry.id)
            .order_by(DerivedPattern.id.desc())
            .first()
        )
        if pattern_row is None:
            job.status = "failed"
            job.error_message = "no derived patterns for entry"
            job.completed_at = datetime.now(timezone.utc)
            return "failed"
        import json as _json

        from ..adapters.engine import PatternSet

        patterns = PatternSet(
            patterns=_json.loads(pattern_row.pattern_json),
            engine_run_id=pattern_row.engine_run_id or "",
            language=entry.language,
        )
        purpose = job.purpose or "scan"
        prior_status = None
        if purpose == "rescan":
            from ..models import CoverageState

            cov_row = (
                sess.query(CoverageState)
                .filter_by(entry_id=entry.id, repository_id=repo.id)
                .one_or_none()
            )
            prior_status = cov_row.status if cov_row else None
        repo_full_name = repo.full_name
        branch = job.branch
        entry_id = entry.id
        repo_id = repo.id
        language = entry.language

    # ---- network phase (no DB transaction held) --------------------------
    try:
        installation_id = repo_installation_id(repo_id)
        token = scm.mint_installation_token(installation_id) if installation_id else None
        archive_dir, sha = fetch_archive_dir(scm, repo, branch, token, work_parent=work_parent)
        context = ScanContext(
            repo_full_name=repo_full_name,
            branch=branch,
            archive_path=archive_dir,
            language=language,
        )
        started = datetime.now(timezone.utc)
        result = engine.scan_repository(context, patterns)
        latency = (datetime.now(timezone.utc) - started).total_seconds()
    except RateLimitError as exc:
        with session_scope() as sess:
            job = sess.get(ScanJob, job_id)
            if job is not None:
                jobs.reenqueue_rate_limited(sess, job, exc.retry_after)
        return "queued"
    except EngineError as exc:
        with session_scope() as sess:
            job = sess.get(ScanJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error_message = f"engine error: {exc}"
                job.completed_at = datetime.now(timezone.utc)
                _fail_coverage(sess, job)
                _notify_engine_failure(sess, entry_id, repo_full_name)
        notifications.flush_pending()  # deliver AFTER the tx commits (CP-2)
        return "failed"
    except (GitHubError, ScanFailure) as exc:
        with session_scope() as sess:
            job = sess.get(ScanJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error_message = str(exc)
                job.completed_at = datetime.now(timezone.utc)
                _fail_coverage(sess, job)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - never let a job wedge the queue
        log.exception("scan job %s crashed", job_id)
        with session_scope() as sess:
            job = sess.get(ScanJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error_message = f"unexpected: {exc}"
                job.completed_at = datetime.now(timezone.utc)
                _fail_coverage(sess, job)
        return "failed"
    finally:
        if archive_dir:
            shutil.rmtree(archive_dir, ignore_errors=True)

    # ---- short write transaction (FR-013) ---------------------------------
    with session_scope() as sess:
        job = sess.get(ScanJob, job_id)
        config_store.record_engine_success(sess)
        now = datetime.now(timezone.utc)
        for occ in result.occurrences:
            sess.add(
                Occurrence(
                    scan_job_id=job.id,
                    file_path=occ.file_path,
                    line_start=occ.line_start,
                    line_end=occ.line_end,
                    snippet=(occ.snippet or "")[:500],
                    confidence=occ.confidence,
                )
            )
        job.occurrences_count = len(result.occurrences)
        job.files_examined = result.files_examined
        if sha:
            job.scanned_sha = sha
        job.status = "done"
        job.completed_at = now
        job.error_message = None
        repo = sess.get(Repository, repo_id)
        if repo is not None:
            repo.last_scan_at = now
        count = len(result.occurrences)
        if purpose == "verification":
            if count == 0:
                apply_transition(
                    sess,
                    entry_id=entry_id,
                    repository_id=repo_id,
                    to_status="migrated",
                    verification_timestamp=now,
                    manual_migrated=False,
                    job_type="verification",
                )
            else:
                apply_transition(
                    sess,
                    entry_id=entry_id,
                    repository_id=repo_id,
                    to_status="migration_incomplete",
                    job_type="verification",
                )
        elif purpose == "rescan":
            if count > 0:
                apply_transition(
                    sess,
                    entry_id=entry_id,
                    repository_id=repo_id,
                    to_status="pattern_detected",
                    job_type="rescan",
                )
                regression_type = "reintroduction" if prior_status == "migrated" else "new_introduction"
                notifications.record_and_deliver(
                    sess,
                    "regression_detected",
                    instance_id=config_store.instance_id(sess),
                    entry_id=entry_id,
                    repository_name=repo_full_name,
                    status="pattern_detected",
                    regression_type=regression_type,
                )
        else:
            apply_transition(
                sess,
                entry_id=entry_id,
                repository_id=repo_id,
                to_status="no_hits" if count == 0 else "pattern_detected",
                job_type="scan",
            )
        notifications.record_and_deliver(
            sess,
            "scan_complete",
            instance_id=config_store.instance_id(sess),
            entry_id=entry_id,
            repository_name=repo_full_name,
            status=f"{count} occurrences",
        )  # queued in-tx; delivered by flush_pending() after commit
        log.info(
            "scan job %s done: repo=%s branch=%s occurrences=%d files=%d latency=%.1fs",
            job_id,
            repo_full_name,
            branch,
            count,
            result.files_examined,
            latency,
        )
    config_store.record_engine_latency_safe(latency)
    notifications.flush_pending()  # deliver AFTER the write tx commits (CP-2)
    return "done"


def _fail_coverage(sess, job) -> None:
    """scanning → scan_failed (FR-065). Verification/rescan failures leave
    coverage untouched (the job itself is retryable from P9)."""
    if (job.purpose or "scan") in ("scan", "manual_rescan"):
        try:
            apply_transition(
                sess,
                entry_id=job.entry_id,
                repository_id=job.repository_id,
                to_status="scan_failed",
                job_type="scan",
                detail=(job.error_message or "")[:400],
            )
        except Exception:  # noqa: BLE001 - transition guard; failure stays on the job row
            log.warning("coverage transition to scan_failed skipped for job %s", job.id)


def repo_installation_id(repository_id: int) -> Optional[int]:
    with session_scope() as sess:
        from ..models import Installation, Repository

        repo = sess.get(Repository, repository_id)
        if repo is None:
            return None
        inst = sess.get(Installation, repo.installation_id)
        return inst.github_installation_id if inst else None
