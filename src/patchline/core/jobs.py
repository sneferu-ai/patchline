"""Scan-job queue orchestration (FR-012/FR-040/FR-048/FR-063/FR-070).

- One job per (repository, branch); default-branch jobs are created first so
  FIFO order within a repository is default-first.
- User-triggered scans priority=10; periodic rescan priority=0.
- Dequeue: atomic claim, ``priority DESC, created_at ASC``, skipping jobs whose
  ``next_attempt_at`` is in the future (rate-limit re-enqueue, FR-070).
- Worker startup requeues jobs claimed >15 minutes ago (crash recovery).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

PRIORITY_USER = 10
PRIORITY_RESCAN = 0
STALE_CLAIM_MINUTES = 15
BRANCH_SCAN_TIMEOUT_MINUTES = 10  # FR-040


def configured_branches(repo) -> List[str]:
    """Repository scan branches: JSON array, defaulting to the default branch;
    capped at 10 by write-path validation (FR-040)."""
    raw = getattr(repo, "scan_branches", None)
    if raw:
        try:
            branches = json.loads(raw)
            if isinstance(branches, list) and branches:
                cleaned = [str(b).strip() for b in branches if str(b).strip()]
                if cleaned:
                    return cleaned[:10]
        except (ValueError, TypeError):
            pass
    return [repo.default_branch]


def enqueue_scans_for_entry(sess, entry, repositories, priority: int = PRIORITY_USER, now: Optional[datetime] = None) -> List:
    """FR-012: one ScanJob per active repository per configured branch.

    Language-mismatched repositories get a job already marked
    ``skipped_language`` (FR-014) and the coverage transition
    not_scanned → skipped_language; default-branch jobs are created first.
    """
    from ..models import ScanJob
    from . import coverage as cov

    now = now or datetime.now(timezone.utc)
    created: List = []
    for repo in repositories:
        if repo.connection_state != "active":  # FR-042
            continue
        branches = configured_branches(repo)
        ordered = sorted(branches, key=lambda b: (b != repo.default_branch, branches.index(b)))
        language_matches = (repo.primary_language or "").strip().lower() == (entry.language or "").strip().lower()
        for branch in ordered:
            # Dedup: an already queued/running job for this (entry, repo, branch)
            # is not recreated on repeated triggers.
            pending = (
                sess.query(ScanJob)
                .filter(
                    ScanJob.entry_id == entry.id,
                    ScanJob.repository_id == repo.id,
                    ScanJob.branch == branch,
                    ScanJob.status.in_(["queued", "running"]),
                )
                .first()
            )
            if pending is not None:
                continue
            # FR-040: non-default-branch jobs carry LOWER priority than the
            # default-branch job for the same repository.
            branch_priority = priority if branch == repo.default_branch else max(priority - 1, 0)
            job = ScanJob(
                entry_id=entry.id,
                repository_id=repo.id,
                branch=branch,
                priority=branch_priority,
                status="queued",
                created_at=now,
            )
            if not language_matches:
                job.status = "skipped_language"
                job.completed_at = now
            sess.add(job)
            sess.flush()
            created.append(job)
        if not language_matches:
            cov.apply_transition(
                sess,
                entry_id=entry.id,
                repository_id=repo.id,
                to_status="skipped_language",
                job_type="scan",
                detail=f"language mismatch: repo={repo.primary_language!r} entry={entry.language!r}",
            )
        else:
            cov.apply_transition(
                sess,
                entry_id=entry.id,
                repository_id=repo.id,
                to_status="scanning",
                job_type="scan",
            )
    return created


def enqueue_rescan(sess, entry, repo, branch: Optional[str] = None, priority: int = PRIORITY_USER, delay_seconds: int = 0):
    """FR-037/FR-024: a single scan job for one (repo, branch) pair."""
    from ..models import ScanJob
    from . import coverage as cov

    job = ScanJob(
        entry_id=entry.id,
        repository_id=repo.id,
        branch=branch or repo.default_branch,
        priority=priority,
        status="queued",
        next_attempt_at=(
            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds) if delay_seconds else None
        ),
    )
    sess.add(job)
    sess.flush()
    cov.apply_transition(
        sess,
        entry_id=entry.id,
        repository_id=repo.id,
        to_status="scanning",
        job_type="scan",
    )
    return job


def claim_next_job(sess, now: Optional[datetime] = None):
    """FR-048: atomic claim. Returns the claimed ScanJob or None.

    Selection: status='queued', next_attempt_at NULL or past,
    ORDER BY priority DESC, created_at ASC. Claim via conditional UPDATE so
    two workers never take the same job.
    """
    from ..models import ScanJob

    now = now or datetime.now(timezone.utc)
    candidates = (
        sess.query(ScanJob)
        .filter(ScanJob.status == "queued")
        .filter((ScanJob.next_attempt_at.is_(None)) | (ScanJob.next_attempt_at <= now))
        .order_by(ScanJob.priority.desc(), ScanJob.created_at.asc(), ScanJob.id.asc())
        .limit(1)
        .all()
    )
    if not candidates:
        return None
    job = candidates[0]
    updated = (
        sess.query(ScanJob)
        .filter(ScanJob.id == job.id, ScanJob.status == "queued")
        .update({"status": "running", "claimed_at": now, "started_at": now}, synchronize_session=False)
    )
    if updated != 1:
        sess.rollback()
        return None
    sess.expire(job)
    return sess.get(ScanJob, job.id)


def enqueue_verification(sess, entry, repo, delay_seconds: int = 5):
    """FR-024: post-merge verification scan.

    Deliberately does NOT transition coverage: the board stays ``pr_open``
    until the verification result lands (FR-065 has no pr_open → scanning
    edge; pr_open → migrated / migration_incomplete fire on completion).
    """
    from ..models import ScanJob

    # FR-024/025: one verification per (entry, repo) at a time — a merged PR
    # "lacking a completed verification" gets exactly one, even on repeat syncs.
    existing = (
        sess.query(ScanJob)
        .filter(
            ScanJob.entry_id == entry.id,
            ScanJob.repository_id == repo.id,
            ScanJob.purpose == "verification",
            ScanJob.status.in_(["queued", "running"]),
        )
        .first()
    )
    if existing is not None:
        return existing
    job = ScanJob(
        entry_id=entry.id,
        repository_id=repo.id,
        branch=repo.default_branch,
        priority=PRIORITY_USER,
        purpose="verification",
        status="queued",
        next_attempt_at=(
            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds) if delay_seconds else None
        ),
    )
    sess.add(job)
    sess.flush()
    return job


def requeue_stale_jobs(sess, stale_minutes: int = STALE_CLAIM_MINUTES, now: Optional[datetime] = None) -> int:
    """Worker startup recovery: jobs claimed longer than the timeout return to
    the queue (FR-048)."""
    from ..models import ScanJob

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=stale_minutes)
    stale = (
        sess.query(ScanJob)
        .filter(ScanJob.status == "running", ScanJob.claimed_at.isnot(None), ScanJob.claimed_at < cutoff)
        .all()
    )
    for job in stale:
        job.status = "queued"
        job.claimed_at = None
        job.started_at = None
        job.error_message = "requeued after stale claim (worker restart)"
        sess.add(job)
    sess.flush()
    return len(stale)


def default_retry_seconds() -> float:
    """Fallback wait when GitHub supplied no Retry-After (FR-070 policy floor)."""
    return 60.0


def reenqueue_rate_limited(sess, job, retry_after: Optional[float]) -> None:
    """FR-070: a rate-limited job goes back to queued with next_attempt_at so
    other repositories proceed while this one waits."""
    delay = retry_after if retry_after and retry_after > 0 else default_retry_seconds()
    job.status = "queued"
    job.claimed_at = None
    job.started_at = None
    job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    job.error_message = f"rate limited; retry after {int(delay)}s"
    sess.add(job)
    sess.flush()


def enqueue_periodic_rescans(sess, entries, repositories_by_id, now: Optional[datetime] = None):
    """FR-063: weekly (configurable) rescan of repositories in migrated or
    no_hits state, priority=0. Returns the created jobs."""
    from ..models import CoverageState, ScanJob

    now = now or datetime.now(timezone.utc)
    created = []
    rows = (
        sess.query(CoverageState)
        .filter(CoverageState.status.in_(["migrated", "no_hits"]))
        .all()
    )
    for row in rows:
        repo = repositories_by_id.get(row.repository_id)
        entry = next((e for e in entries if e.id == row.entry_id), None)
        if repo is None or entry is None:
            continue
        if repo.connection_state != "active" or entry.status != "ready":
            continue
        already = (
            sess.query(ScanJob)
            .filter(
                ScanJob.entry_id == row.entry_id,
                ScanJob.repository_id == row.repository_id,
                ScanJob.status.in_(["queued", "running"]),
            )
            .first()
        )
        if already:
            continue
        job = ScanJob(
            entry_id=row.entry_id,
            repository_id=row.repository_id,
            branch=repo.default_branch,
            priority=PRIORITY_RESCAN,
            status="queued",
            created_at=now,
        )
        sess.add(job)
        sess.flush()
        created.append(job)
    return created
