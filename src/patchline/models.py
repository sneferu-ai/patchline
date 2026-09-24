"""SQLAlchemy ORM models — §5 data model of the B13 contract.

Rules encoded here (FR-053):
- Every table carries ``tenant_id`` (default 1, forward-compatibility only).
- Soft-delete-capable tables carry nullable ``deleted_at``.
- EXEMPT from ``deleted_at``: DerivedPattern (immutable), Config (key-value),
  CoverageState (derived state), Confirmation (append-only audit — NEVER deleted).
- ScanJob carries ``priority`` and ``next_attempt_at`` (FR-012/FR-070).
- CoverageState has the composite ``idx_coverage_repo_status`` index (FR-047/066).
- Session carries two additive columns beyond the spec's listed minimum:
  ``csrf_token`` (FR-031 "store in session") and ``oauth_state`` (FR-032
  "store in session before redirect"; pre-auth sessions have vendor_id NULL).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Vendor(Base):
    __tablename__ = "vendor"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    github_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    github_login: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Session(Base):
    __tablename__ = "session"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    vendor_id: Mapped[int] = mapped_column(Integer, nullable=True)  # NULL for pre-auth (OAuth state) sessions
    cookie_value: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Additive session-scoped security material (FR-031 / FR-032).
    csrf_token: Mapped[str] = mapped_column(String(255), nullable=True)
    oauth_state: Mapped[str] = mapped_column(String(255), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Installation(Base):
    __tablename__ = "installation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    github_installation_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    account_login: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str] = mapped_column(String(64), default="User", nullable=False)
    connection_state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Repository(Base):
    __tablename__ = "repository"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    installation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    full_name: Mapped[str] = mapped_column(String(512), nullable=False)  # owner/repo
    default_branch: Mapped[str] = mapped_column(String(255), default="main", nullable=False)
    primary_language: Mapped[str] = mapped_column(String(64), nullable=True)
    scan_branches: Mapped[str] = mapped_column(Text, nullable=True)  # JSON array, max 10 (FR-040)
    connection_state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    last_scan_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class ChangelogEntry(Base):
    __tablename__ = "changelog_entry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    vendor_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=True)  # two-step insert (FR-073)
    prior_api_shape: Mapped[str] = mapped_column(Text, nullable=False)
    target_contract: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)  # draft/ready/derivation_failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class DerivedPattern(Base):
    """Immutable once written; EXEMPT from deleted_at (FR-053)."""

    __tablename__ = "derived_pattern"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    entry_id: Mapped[int] = mapped_column(Integer, nullable=False)
    pattern_json: Mapped[str] = mapped_column(Text, nullable=False)  # opaque blob; engine adapter only
    engine_run_id: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScanJob(Base):
    __tablename__ = "scan_job"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    entry_id: Mapped[int] = mapped_column(Integer, nullable=False)
    repository_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)  # one job per branch (FR-012)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # user=10, rescan=0
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)  # FR-070
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)  # queued/running/done/failed/skipped_language
    purpose: Mapped[str] = mapped_column(String(32), default="scan", nullable=False)  # scan/manual_rescan/verification/rescan (additive)
    occurrences_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    files_examined: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scanned_sha: Mapped[str] = mapped_column(String(64), nullable=True)  # HEAD sha scanned (archive re-fetch, FR-016)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Occurrence(Base):
    __tablename__ = "occurrence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scan_job_id: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    line_start: Mapped[int] = mapped_column(Integer, nullable=False)
    line_end: Mapped[int] = mapped_column(Integer, nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=True)  # truncated to 500 chars (§5 privacy)
    confidence: Mapped[str] = mapped_column(String(32), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Remediation(Base):
    __tablename__ = "remediation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scan_job_id: Mapped[int] = mapped_column(Integer, nullable=True)
    repository_id: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_id: Mapped[int] = mapped_column(Integer, nullable=False)
    diff_content: Mapped[str] = mapped_column(Text, nullable=True)
    diff_sha256: Mapped[str] = mapped_column(String(64), nullable=True)
    provenance_json: Mapped[str] = mapped_column(Text, nullable=True)
    engine_run_id: Mapped[str] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="generating", nullable=False)  # generating/ready/generation_failed/dispatch_failed/dispatched
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Confirmation(Base):
    """Append-only audit event. NEVER deleted; NO deleted_at column (FR-053, CP-7)."""

    __tablename__ = "confirmation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    remediation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_id: Mapped[int] = mapped_column(Integer, nullable=False)
    repository_id: Mapped[int] = mapped_column(Integer, nullable=False)
    vendor_id: Mapped[int] = mapped_column(Integer, nullable=False)
    diff_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PullRequest(Base):
    __tablename__ = "pull_request"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    remediation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    repository_id: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_name: Mapped[str] = mapped_column(String(255), nullable=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str] = mapped_column(String(1024), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False)  # open/merged/closed/rejected/dispatch_failed
    manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # diff-export path badge (FR-067)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class JobLog(Base):
    __tablename__ = "job_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    detail: Mapped[str] = mapped_column(Text, nullable=True)  # e.g. coverage transition "from -> to"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookDelivery(Base):
    __tablename__ = "webhook_delivery"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    github_delivery_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="processing", nullable=False)  # processing/succeeded/failed
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("github_delivery_id", name="uq_webhook_delivery_id"),)


class Notification(Base):
    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_url: Mapped[str] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)  # pending/sent/failed/disabled
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(64), nullable=False)  # UUID for receiver idempotency
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class FeedbackFlag(Base):
    """Exempt from the cleanup CLI (retained indefinitely, FR-049) though it
    still carries a deleted_at column per FR-053's table-wide rule."""

    __tablename__ = "feedback_flag"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    occurrence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    vendor_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Config(Base):
    """Key-value instance configuration. EXEMPT from deleted_at (FR-053)."""

    __tablename__ = "config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class CoverageState(Base):
    """One row per (entry, repository). Derived state; EXEMPT from deleted_at.
    Transitions follow the FR-065 state machine and are logged in JobLog."""

    __tablename__ = "coverage_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    entry_id: Mapped[int] = mapped_column(Integer, nullable=False)
    repository_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="not_scanned", nullable=False)
    verification_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    manual_migrated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("entry_id", "repository_id", name="uq_coverage_entry_repo"),
        Index("idx_coverage_repo_status", "repository_id", "status"),
    )
