"""Initial schema (all tables + idx_coverage_repo_status).

Revision ID: 0001
Revises:
Create Date: 2026-08-05

The initial migration materializes the complete §5 data model: every table
carries tenant_id; soft-delete-capable tables carry deleted_at; exempt tables
(DerivedPattern, Config, CoverageState, Confirmation) do not; ScanJob carries
priority/next_attempt_at; CoverageState carries the composite
idx_coverage_repo_status index (FR-047/FR-066). Index and table definitions
are the single source of truth in patchline.models — the migration simply
materializes them so the ORM and the schema can never drift.
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from patchline import models

    models.Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from patchline import models

    models.Base.metadata.drop_all(bind=op.get_bind())
