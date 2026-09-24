"""P11 — operator CLI (FR-029). Entry: ``python -m patchline.cli``.

Subcommands: migrate, set-default-language, export-pilot-evidence, cleanup,
verify-config, revoke-sessions, generate-fixture, migrate-fixtures.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

from . import APP_VERSION, config

log = logging.getLogger("patchline.cli")

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
ALEMBIC_VERSIONS_DIR = os.path.join(REPO_ROOT, "alembic", "versions")
ENGINE_FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "engine")

DESTRUCTIVE_PATTERNS = (
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE),
    re.compile(r"\bALTER\s+TABLE\b[^;\n]*\bDROP\b", re.IGNORECASE),
    re.compile(r"\bop\.drop_table\s*\(", re.IGNORECASE),
    re.compile(r"\bop\.drop_column\s*\(", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# migrate


def _pending_migration_sources() -> list:
    """Source texts of alembic versions not yet applied (best effort).

    Falls back to scanning every version file when the revision state cannot
    be determined — the destructive gate stays conservative.
    """
    sources = []
    if not os.path.isdir(ALEMBIC_VERSIONS_DIR):
        return sources
    applied = set()
    try:
        from sqlalchemy import text

        from .db import get_engine

        with get_engine().connect() as conn:
            try:
                rows = conn.execute(text("SELECT version_num FROM alembic_version")).all()
                applied = {r[0] for r in rows}
            except Exception:  # noqa: BLE001 - table absent (fresh DB)
                applied = set()
    except Exception:  # noqa: BLE001 - sqlalchemy unavailable: stay conservative
        applied = set()
    for name in sorted(os.listdir(ALEMBIC_VERSIONS_DIR)):
        if not name.endswith(".py"):
            continue
        revision = name.split("_", 1)[0]
        if revision in applied:
            continue
        path = os.path.join(ALEMBIC_VERSIONS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                sources.append((name, fh.read()))
        except OSError:
            pass
    return sources


def _has_destructive_ops(sources) -> list:
    hits = []
    for name, text_ in sources:
        for pattern in DESTRUCTIVE_PATTERNS:
            if pattern.search(text_):
                hits.append(name)
                break
    return hits


def cmd_migrate(args) -> int:
    """FR-029: destructive migrations (DROP TABLE / DROP COLUMN / ALTER TABLE
    ... DROP) require --allow-destructive or ALLOW_DESTRUCTIVE_MIGRATIONS.
    ALTER TABLE ... ADD COLUMN is additive and never gated."""
    allow = args.allow_destructive or config.allow_destructive_migrations()
    pending = _pending_migration_sources()
    destructive = _has_destructive_ops(pending)
    if destructive and not allow:
        print(
            "Refusing to run destructive migrations without --allow-destructive "
            f"(or ALLOW_DESTRUCTIVE_MIGRATIONS): {', '.join(destructive)}",
            file=sys.stderr,
        )
        return 1
    try:
        from alembic import command
        from alembic.config import Config as AlembicConfig

        cfg = AlembicConfig(os.path.join(REPO_ROOT, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "alembic"))
        cfg.set_main_option("sqlalchemy.url", config.database_url())
        command.upgrade(cfg, "head")
        print("migrate: alembic upgrade head complete")
        return 0
    except ImportError:
        # Dev-sandbox fallback: create the schema directly (alembic absent).
        log.warning("alembic not installed; falling back to metadata create_all")
        from patchline import models
        from patchline.db import get_engine

        models.Base.metadata.create_all(get_engine())
        print("migrate: schema created via metadata create_all (alembic unavailable)")
        return 0


# ---------------------------------------------------------------------------
# set-default-language


def cmd_set_default_language(args) -> int:
    from .adapters.languages import is_installed
    from .core.config_store import set_config_value
    from .db import session_scope

    pack = args.pack.strip().lower()
    if not is_installed(pack):
        print(f"language pack {pack!r} is not installed", file=sys.stderr)
        return 1
    with session_scope() as sess:
        set_config_value(sess, "default_language", pack)
    print(f"default language set to {pack!r} (Config table; takes precedence over DEFAULT_LANGUAGE env)")
    return 0


# ---------------------------------------------------------------------------
# export-pilot-evidence (FR-051 normative schema)


def cmd_export_pilot_evidence(args) -> int:
    from .core import config_store
    from .core.coverage import STATUSES
    from .db import session_scope
    from .models import (
        ChangelogEntry,
        Confirmation,
        CoverageState,
        DerivedPattern,
        PullRequest,
        Repository,
        ScanJob,
        WebhookDelivery,
    )

    with session_scope() as sess:
        engine_mode = "mock" if config.demo_mode() else config.engine_mode()
        try:
            from .adapters.engine import create_adapter

            engine_version = create_adapter().get_version()
        except Exception:  # noqa: BLE001
            engine_version = None

        status_counts = {s: 0 for s in STATUSES}
        for row in sess.query(CoverageState).all():
            status_counts[row.status] = status_counts.get(row.status, 0) + 1

        entries = []
        for entry in sess.query(ChangelogEntry).filter(ChangelogEntry.deleted_at.is_(None)).all():
            pattern = (
                sess.query(DerivedPattern)
                .filter_by(entry_id=entry.id)
                .order_by(DerivedPattern.id.desc())
                .first()
            )
            scan_jobs = (
                sess.query(ScanJob)
                .filter(ScanJob.entry_id == entry.id, ScanJob.deleted_at.is_(None))
                .all()
            )
            repos_scanned = {j.repository_id for j in scan_jobs if j.status == "done"}
            repos_hits = {j.repository_id for j in scan_jobs if j.status == "done" and j.occurrences_count > 0}
            prs = (
                sess.query(PullRequest)
                .filter(PullRequest.entry_id == entry.id, PullRequest.deleted_at.is_(None))
                .all()
            )
            entries.append(
                {
                    "id": entry.id,
                    "title": entry.title,
                    "language": entry.language,
                    "status": entry.status,
                    "created_at": entry.created_at.isoformat() if entry.created_at else None,
                    "derived_pattern_engine_run_id": pattern.engine_run_id if pattern else None,
                    "repositories_scanned": len(repos_scanned),
                    "repositories_with_hits": len(repos_hits),
                    "prs_opened": len(prs),
                    "prs_merged": len([p for p in prs if p.state == "merged"]),
                }
            )
        repositories = [
            {
                "id": repo.id,
                "full_name": repo.full_name,
                "connection_state": repo.connection_state,
                "primary_language": repo.primary_language,
                "last_scan_at": repo.last_scan_at.isoformat() if repo.last_scan_at else None,
            }
            for repo in sess.query(Repository).filter(Repository.deleted_at.is_(None)).all()
        ]
        pull_requests = []
        for pr in sess.query(PullRequest).filter(PullRequest.deleted_at.is_(None)).all():
            repo = sess.get(Repository, pr.repository_id)
            entry = sess.get(ChangelogEntry, pr.entry_id)
            pull_requests.append(
                {
                    "id": pr.id,
                    "repository_full_name": repo.full_name if repo else None,
                    "entry_title": entry.title if entry else None,
                    "pr_number": pr.pr_number,
                    "pr_url": pr.pr_url,
                    "state": pr.state,
                    "opened_at": pr.opened_at.isoformat() if pr.opened_at else None,
                }
            )
        confirmations = [
            {
                "id": row.id,
                "remediation_id": row.remediation_id,
                "entry_id": row.entry_id,
                "repository_id": row.repository_id,
                "diff_sha256": row.diff_sha256,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in sess.query(Confirmation).all()
        ]
        deliveries = sess.query(WebhookDelivery).all()
        export = {
            "instance": {
                "app_version": APP_VERSION,
                "engine_mode": engine_mode,
                "engine_version": engine_version,
                "demo_mode": config.demo_mode(),
                "instance_id": config_store.instance_id(sess),
                "exported_at": datetime.now(timezone.utc).isoformat(),
            },
            "coverage_summary": {
                "total_entries": len(entries),
                "total_repositories": len(repositories),
                "status_counts": status_counts,
            },
            "entries": entries,
            "repositories": repositories,
            "pull_requests": pull_requests,
            "confirmations": confirmations,
            "webhook_deliveries": {
                "total": len(deliveries),
                "succeeded": len([d for d in deliveries if d.status == "succeeded"]),
                "failed": len([d for d in deliveries if d.status == "failed"]),
            },
        }
    json.dump(export, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------
# cleanup (FR-049): soft-delete; exemptions honored.

# Tables whose date filter is NOT ``created_at`` (§5 data model):
#   Occurrence  — no date column at all; filter by parent ScanJob.created_at
#   PullRequest — uses ``opened_at`` (PR open time), not created_at
_DATE_COLUMN = {
    "PullRequest": "opened_at",
}


SOFT_DELETE_TABLES = (
    "ChangelogEntry",
    "ScanJob",
    "Occurrence",
    "Remediation",
    "PullRequest",
    "JobLog",
    "WebhookDelivery",
    "Notification",
)


def cmd_cleanup(args) -> int:
    from . import models
    from .db import session_scope

    try:
        before = datetime.strptime(args.before, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        print("--before must be YYYY-MM-DD", file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    counts = {}
    with session_scope() as sess:
        for table_name in SOFT_DELETE_TABLES:
            model = getattr(models, table_name)
            if table_name == "Occurrence":
                # Occurrence has no date column (§5); filter by parent
                # ScanJob's created_at.
                ScanJob = models.ScanJob
                rows = (
                    sess.query(model)
                    .join(ScanJob, model.scan_job_id == ScanJob.id)
                    .filter(ScanJob.created_at < before, model.deleted_at.is_(None))
                    .all()
                )
            else:
                date_col = getattr(model, _DATE_COLUMN.get(table_name, "created_at"))
                rows = (
                    sess.query(model)
                    .filter(date_col < before, model.deleted_at.is_(None))
                    .all()
                )
            for row in rows:
                row.deleted_at = now
            counts[table_name] = len(rows)
        # FR-049/FR-039: FeedbackFlag rows are EXEMPT — retained indefinitely.
        # Confirmation rows are NEVER deleted (append-only audit).
    print(f"cleanup: soft-deleted records older than {args.before}: {json.dumps(counts)}")
    print("cleanup: confirmations and feedback flags untouched (retained indefinitely)")
    return 0


# ---------------------------------------------------------------------------
# verify-config


REQUIRED_ENV_VARS = (
    "GITHUB_APP_ID",
    "GITHUB_APP_SLUG",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_ALLOWLISTED_LOGIN",
    "ENGINE_MODE",
    "SESSION_SECRET",
    "DATABASE_URL",
    "DEFAULT_LANGUAGE",
)


def verify_config_errors() -> tuple:
    """(errors, warnings) — shared by the CLI and tests."""
    errors = []
    warnings = []
    for name in REQUIRED_ENV_VARS:
        if not (os.environ.get(name) or "").strip():
            errors.append(f"{name} is not set")
    if not ((os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH") or "").strip() or (os.environ.get("GITHUB_APP_PRIVATE_KEY") or "").strip()):
        errors.append("GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY must be set")
    allow = config.allowlisted_login()
    if allow is None:
        errors.append("GITHUB_ALLOWLISTED_LOGIN is unset or empty (misconfigured allowlist)")
    url = config.notification_webhook_url()
    secret = config.notification_signing_secret()
    if bool(url) != bool(secret):
        errors.append(
            "NOTIFICATION_WEBHOOK_URL and NOTIFICATION_SIGNING_SECRET must be set together "
            "(exactly one is set; notifications disabled until paired)"
        )
    if config.engine_mode() == "sneferu":
        if not config.sneferu_base_url():
            errors.append("SNEFERU_BASE_URL is required when ENGINE_MODE=sneferu")
        if not config.sneferu_service_token():
            errors.append("SNEFERU_SERVICE_TOKEN is required when ENGINE_MODE=sneferu")
    try:
        from .db import session_scope
        from .models import Repository

        cap = config.max_repos_per_installation()
        with session_scope() as sess:
            counts = {}
            for repo in sess.query(Repository).filter(Repository.deleted_at.is_(None)).all():
                counts[repo.installation_id] = counts.get(repo.installation_id, 0) + 1
            for installation_id, n in counts.items():
                if n > cap:
                    warnings.append(
                        f"installation {installation_id} has {n} repositories (> MAX_REPOS_PER_INSTALLATION={cap}); "
                        "GitHub rate limits may be hit during full scans"
                    )
    except Exception:  # noqa: BLE001 - DB not migrated yet: skip the warning check
        pass
    return errors, warnings


def cmd_verify_config(args) -> int:
    errors, warnings = verify_config_errors()
    for warning in warnings:
        print(f"WARN: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("verify-config: OK")
    return 0


# ---------------------------------------------------------------------------
# revoke-sessions (FR-064)


def cmd_revoke_sessions(args) -> int:
    from .auth import revoke_all_sessions
    from .db import session_scope

    with session_scope() as sess:
        count = revoke_all_sessions(sess)
    print(f"revoke-sessions: revoked {count} active session(s)")
    return 0


# ---------------------------------------------------------------------------
# Fixture helpers (FR-045)


def cmd_generate_fixture(args) -> int:
    os.makedirs(ENGINE_FIXTURE_DIR, exist_ok=True)
    name = args.repo or "default"
    from .adapters.engine import sanitize_repo_key

    path = os.path.join(ENGINE_FIXTURE_DIR, f"{sanitize_repo_key(name)}.json")
    template = {
        "fixture_version": "1.0",
        "patterns": [{"kind": "TODO", "old": "old_call", "new": "new_call"}],
        "occurrences": [
            {
                "file_path": "src/example.py",
                "line_start": 1,
                "line_end": 1,
                "snippet": "TODO: code line containing the old pattern",
                "confidence": "high",
            }
        ],
        "diff": "--- a/src/example.py\n+++ b/src/example.py\n@@ -1,1 +1,1 @@\n-TODO old line\n+TODO new line\n",
        "provenance": {
            "quality_score": 0.0,
            "certificate_class": "TODO",
            "disagreement_record": "TODO",
            "models": ["TODO"],
            "extra": {},
        },
    }
    if args.entry:
        template["_entry_text"] = args.entry
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(template, fh, indent=2)
    print(f"generate-fixture: wrote template {path} — fill in the TODO placeholders")
    return 0


def cmd_migrate_fixtures(args) -> int:
    """Upgrade old-schema fixture JSON where possible; flag the rest (FR-045)."""
    upgraded, flagged = [], []
    if not os.path.isdir(ENGINE_FIXTURE_DIR):
        print("migrate-fixtures: no fixture directory")
        return 0
    for name in sorted(os.listdir(ENGINE_FIXTURE_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(ENGINE_FIXTURE_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            flagged.append(f"{name}: unreadable JSON — regenerate with generate-fixture")
            continue
        version = str(data.get("fixture_version", ""))
        if version == "1.0":
            continue
        if isinstance(data, dict) and ("patterns" in data or "occurrences" in data or "diff" in data):
            data["fixture_version"] = "1.0"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            upgraded.append(name)
        else:
            flagged.append(f"{name}: unrecognized schema — regenerate manually with generate-fixture")
    print(f"migrate-fixtures: upgraded {len(upgraded)} fixture(s)")
    for item in flagged:
        print(f"FLAG: {item}")
    return 1 if flagged else 0


# ---------------------------------------------------------------------------
# entrypoint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="patchline", description="Patchline operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="run database migrations")
    p.add_argument("--allow-destructive", action="store_true")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("set-default-language", help="set the default language pack")
    p.add_argument("pack")
    p.set_defaults(func=cmd_set_default_language)

    p = sub.add_parser("export-pilot-evidence", help="emit pilot evidence JSON")
    p.set_defaults(func=cmd_export_pilot_evidence)

    p = sub.add_parser("cleanup", help="soft-delete records older than a date")
    p.add_argument("--before", required=True, help="YYYY-MM-DD")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("verify-config", help="validate environment configuration")
    p.set_defaults(func=cmd_verify_config)

    p = sub.add_parser("revoke-sessions", help="revoke all active sessions")
    p.set_defaults(func=cmd_revoke_sessions)

    p = sub.add_parser("generate-fixture", help="create a template engine fixture")
    p.add_argument("--entry", default=None)
    p.add_argument("--repo", default=None)
    p.set_defaults(func=cmd_generate_fixture)

    p = sub.add_parser("migrate-fixtures", help="upgrade old-schema fixtures")
    p.set_defaults(func=cmd_migrate_fixtures)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
