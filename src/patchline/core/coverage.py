"""FR-065 coverage board state machine + FR-066 roll-up helpers.

The transition map is PURE (no DB) so the full machine is unit-testable;
``apply_transition`` performs the validated DB write and logs the audit row
in JobLog (FR-065 note: all transitions are logged).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional, Set, Tuple

STATUSES = (
    "not_scanned",
    "scanning",
    "no_hits",
    "pattern_detected",
    "remediation_pending",
    "pr_open",
    "pr_rejected",
    "migrated",
    "migration_incomplete",
    "scan_failed",
    "dispatch_failed",
    "skipped_language",
)

INITIAL_STATUS = "not_scanned"

# FR-066: statuses counted as "open changes" in the home roll-up
# (pr_rejected IS included — a rejected PR still requires vendor action).
OPEN_CHANGE_STATUSES: Set[str] = {
    "pattern_detected",
    "remediation_pending",
    "pr_open",
    "pr_rejected",
    "migration_incomplete",
    "dispatch_failed",
}

# FR-065 transition table. not_scanned is the INITIAL state only and is not
# reachable after entry creation (entries are immutable, FR-041; no delete).
TRANSITIONS: Dict[str, Set[str]] = {
    "not_scanned": {"scanning", "skipped_language"},
    "scanning": {"no_hits", "pattern_detected", "scan_failed", "skipped_language"},
    "no_hits": {"scanning", "pattern_detected"},
    "pattern_detected": {"remediation_pending", "scanning"},
    "remediation_pending": {"pr_open", "dispatch_failed", "scanning"},
    "pr_open": {"migrated", "migration_incomplete", "pr_rejected"},
    "pr_rejected": {"scanning", "remediation_pending"},
    "migrated": {"pattern_detected", "scanning"},
    "migration_incomplete": {"remediation_pending", "scanning", "migrated"},
    "scan_failed": {"scanning"},
    "dispatch_failed": {"pr_open", "remediation_pending", "scanning", "migrated"},
    "skipped_language": {"scanning"},
}


class InvalidTransition(Exception):
    def __init__(self, from_status: str, to_status: str):
        super().__init__(f"invalid coverage transition: {from_status!r} -> {to_status!r}")
        self.from_status = from_status
        self.to_status = to_status


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in TRANSITIONS.get(from_status, set())


def assert_transition(from_status: str, to_status: str) -> None:
    if not can_transition(from_status, to_status):
        raise InvalidTransition(from_status, to_status)


def is_open_change(status: str) -> bool:
    return status in OPEN_CHANGE_STATUSES


def apply_transition(
    sess,
    *,
    entry_id: int,
    repository_id: int,
    to_status: str,
    verification_timestamp: Optional[datetime] = None,
    manual_migrated: Optional[bool] = None,
    job_type: str = "coverage",
    detail: Optional[str] = None,
) -> Tuple[object, str]:
    """Validate and apply a coverage transition; write the JobLog audit row.

    Creates the CoverageState row on first use (from the initial state).
    Returns (row, from_status). The caller owns the surrounding transaction.
    """
    from ..models import CoverageState, JobLog  # deferred: keeps the map pure

    row = (
        sess.query(CoverageState)
        .filter_by(entry_id=entry_id, repository_id=repository_id)
        .one_or_none()
    )
    if row is None:
        row = CoverageState(entry_id=entry_id, repository_id=repository_id, status=INITIAL_STATUS)
        sess.add(row)
        sess.flush()
    from_status = row.status
    if from_status == to_status:
        return row, from_status  # idempotent no-op (e.g. duplicate webhook)
    assert_transition(from_status, to_status)
    row.status = to_status
    row.last_updated_at = datetime.now(timezone.utc)
    if verification_timestamp is not None:
        row.verification_timestamp = verification_timestamp
    if manual_migrated is not None:
        row.manual_migrated = manual_migrated
    if to_status == "migrated" and manual_migrated is None:
        # Verified path always refreshes the stamp and clears any manual flag.
        row.manual_migrated = False
        if verification_timestamp is not None:
            row.verification_timestamp = verification_timestamp
    sess.add(
        JobLog(
            job_type=job_type,
            entity_id=row.id,
            status=to_status,
            detail=detail or f"coverage {from_status} -> {to_status} (entry={entry_id}, repo={repository_id})",
        )
    )
    sess.flush()
    return row, from_status


def rollup_counts(sess) -> Dict[str, int]:
    """status -> count across the whole board (FR-051 coverage_summary)."""
    from ..models import CoverageState

    counts = {status: 0 for status in STATUSES}
    for status, n in sess.query(CoverageState.status, CoverageState.id).all():
        counts[status] = counts.get(status, 0) + 1
    return counts
