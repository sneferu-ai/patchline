"""Schema verification (AC-001 support): tables, key columns, exemptions, index."""
from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

EXPECTED_TABLES = {
    "vendor",
    "session",
    "installation",
    "repository",
    "changelog_entry",
    "derived_pattern",
    "scan_job",
    "occurrence",
    "remediation",
    "confirmation",
    "pull_request",
    "job_log",
    "webhook_delivery",
    "notification",
    "feedback_flag",
    "config",
    "coverage_state",
}

# FR-053: exempt from deleted_at.
EXEMPT_FROM_DELETED_AT = {"derived_pattern", "config", "coverage_state", "confirmation"}
SOFT_DELETE_TABLES = EXPECTED_TABLES - EXEMPT_FROM_DELETED_AT


class TestSchema:
    def test_all_tables_created(self, db):
        from sqlalchemy import inspect

        tables = set(inspect(db).get_table_names())
        assert EXPECTED_TABLES <= tables

    def test_composite_index_exists(self, db):
        from sqlalchemy import inspect

        indexes = {idx["name"] for idx in inspect(db).get_indexes("coverage_state")}
        assert "idx_coverage_repo_status" in indexes

    def test_tenant_id_everywhere(self, db):
        from sqlalchemy import inspect

        inspector = inspect(db)
        for table in EXPECTED_TABLES:
            columns = {c["name"] for c in inspector.get_columns(table)}
            assert "tenant_id" in columns, f"{table} missing tenant_id"

    def test_deleted_at_contract(self, db):
        from sqlalchemy import inspect

        inspector = inspect(db)
        for table in SOFT_DELETE_TABLES:
            columns = {c["name"] for c in inspector.get_columns(table)}
            assert "deleted_at" in columns, f"{table} must have deleted_at"
        for table in EXEMPT_FROM_DELETED_AT:
            columns = {c["name"] for c in inspector.get_columns(table)}
            assert "deleted_at" not in columns, f"{table} must NOT have deleted_at (FR-053)"

    def test_scan_job_columns(self, db):
        from sqlalchemy import inspect

        columns = {c["name"] for c in inspect(db).get_columns("scan_job")}
        assert {"priority", "next_attempt_at", "branch", "claimed_at"} <= columns

    def test_webhook_delivery_unique(self, db):
        from sqlalchemy import inspect

        uniques = inspect(db).get_unique_constraints("webhook_delivery")
        assert any("github_delivery_id" in c.get("column_names", []) for c in uniques)
