"""GET surfaces P1–P10 (server-rendered HTML; htmx progressive enhancement).

Every list surface defines empty/loading/error states; every asynchronous
region uses aria-live="polite"; status is always icon + text (never color
alone). All data reads use the read-only connection pool (CP-2).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import APP_VERSION, config
from ..adapters.engine import Provenance
from ..adapters.languages import installed_pack_names
from ..core import config_store
from ..core.coverage import OPEN_CHANGE_STATUSES
from ..db import session_scope
from ..webutil import base_context

router = APIRouter()

STATUS_META = {
    "not_scanned": ("○", "Not scanned"),
    "scanning": ("…", "Scanning"),
    "no_hits": ("✓", "No occurrences"),
    "pattern_detected": ("⚠", "Pattern detected"),
    "remediation_pending": ("◐", "Remediation pending"),
    "pr_open": ("↗", "PR open"),
    "pr_rejected": ("✗", "PR rejected"),
    "migrated": ("✔", "Migrated"),
    "migration_incomplete": ("⚠", "Migration incomplete"),
    "scan_failed": ("✗", "Scan failed"),
    "dispatch_failed": ("✗", "Dispatch failed"),
    "skipped_language": ("⊘", "Skipped (language)"),
}


def status_label(row) -> str:
    """P8 display rules: migrated carries a verification timestamp or the
    manual flag; re-verify hint past 7 days."""
    icon, label = STATUS_META.get(row.status, ("?", row.status))
    if row.status == "migrated":
        if getattr(row, "manual_migrated", False):
            return f"{icon} migrated (manual)"
        ts = getattr(row, "verification_timestamp", None)
        if ts:
            label = f"migrated (verified {_aware(ts).date().isoformat()})"
            if datetime.now(timezone.utc) - _aware(ts) > timedelta(days=7):
                label += " — re-verify suggested"
            return f"{icon} {label}"
        return f"{icon} migrated"
    return f"{icon} {label}"


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _t(request: Request):
    return request.app.state.templates


def _or_404(request: Request, message: str):
    return _t(request).TemplateResponse(
        request,
        "error.html",
        base_context(request, status=404, message=message),
        status_code=404,
    )


# ---------------------------------------------------------------------------
# P8 — home roll-up (FR-066)


@router.get("/")
def home(request: Request):
    from ..models import CoverageState, Installation, Repository

    with session_scope(read_only=True) as sess:
        from ..models import ChangelogEntry

        repos = sess.query(Repository).filter(Repository.deleted_at.is_(None)).all()
        installations = {i.id: i for i in sess.query(Installation).all()}
        rows = sess.query(CoverageState).all()
        total_entries = sess.query(ChangelogEntry).filter(ChangelogEntry.deleted_at.is_(None)).count()
        by_repo = {}
        for row in rows:
            bucket = by_repo.setdefault(row.repository_id, {"open": 0, "migrated": 0, "not_scanned": 0, "seen": 0})
            bucket["seen"] += 1
            if row.status in OPEN_CHANGE_STATUSES:
                bucket["open"] += 1
            elif row.status == "migrated":
                bucket["migrated"] += 1
            elif row.status == "not_scanned":
                bucket["not_scanned"] += 1
        items = []
        for repo in repos:
            inst = installations.get(repo.installation_id)
            counts = by_repo.get(repo.id, {"open": 0, "migrated": 0, "not_scanned": 0, "seen": 0})
            # Entries with no CoverageState row at all are "not yet scanned" too.
            not_scanned = counts["not_scanned"] + max(total_entries - counts["seen"], 0)
            items.append(
                {
                    "id": repo.id,
                    "full_name": repo.full_name,
                    "account": inst.account_login if inst else "unknown",
                    "connection_state": repo.connection_state,
                    "open_changes": counts["open"],
                    "migrated": counts["migrated"],
                    "not_scanned": not_scanned,
                }
            )
    return _t(request).TemplateResponse(
        request,
        "coverage_home.html",
        base_context(request, items=items, total_entries=total_entries),
    )


# ---------------------------------------------------------------------------
# P8 — per-entry coverage detail


@router.get("/entries/{entry_id}/coverage")
def entry_coverage(request: Request, entry_id: int):
    from ..models import ChangelogEntry, CoverageState, Repository

    with session_scope(read_only=True) as sess:
        entry = sess.get(ChangelogEntry, entry_id)
        if entry is None:
            return _or_404(request, "Entry not found")
        repos = sess.query(Repository).filter(Repository.deleted_at.is_(None)).all()
        states = {
            row.repository_id: row
            for row in sess.query(CoverageState).filter_by(entry_id=entry_id).all()
        }
        items = []
        for repo in repos:
            row = states.get(repo.id)
            if row is None:
                label = f"{STATUS_META['not_scanned'][0]} Not scanned"
                status = "not_scanned"
            else:
                label = status_label(row)
                status = row.status
            items.append({"repo": repo, "status": status, "label": label})
    return _t(request).TemplateResponse(
        request,
        "coverage_entry.html",
        base_context(request, entry=entry, items=items),
    )


# ---------------------------------------------------------------------------
# P8b — per-repository coverage detail


@router.get("/repositories/{rid}/coverage")
def repo_coverage(request: Request, rid: int):
    from ..models import ChangelogEntry, CoverageState, Repository

    with session_scope(read_only=True) as sess:
        repo = sess.get(Repository, rid)
        if repo is None:
            return _or_404(request, "Repository not found")
        rows = sess.query(CoverageState).filter_by(repository_id=rid).all()
        items = []
        for row in rows:
            entry = sess.get(ChangelogEntry, row.entry_id)
            items.append(
                {
                    "entry": entry,
                    "status": row.status,
                    "label": status_label(row),
                    "last_updated_at": row.last_updated_at,
                }
            )
    return _t(request).TemplateResponse(
        request,
        "coverage_repo.html",
        base_context(request, repo=repo, items=items),
    )


# ---------------------------------------------------------------------------
# P2 — connection setup


@router.get("/connections/setup")
def connections_setup(request: Request):
    from ..models import Installation, Repository

    slug = os.environ.get("GITHUB_APP_SLUG") or "your-app"
    install_url = f"https://github.com/apps/{slug}/installations/new"
    with session_scope(read_only=True) as sess:
        installations = sess.query(Installation).filter(Installation.deleted_at.is_(None)).all()
        counts = {}
        for repo in sess.query(Repository).filter(Repository.deleted_at.is_(None)).all():
            counts[repo.installation_id] = counts.get(repo.installation_id, 0) + 1
    return _t(request).TemplateResponse(
        request,
        "connections.html",
        base_context(request, install_url=install_url, installations=installations, repo_counts=counts),
    )


# ---------------------------------------------------------------------------
# P3 — repository inventory (FR-007/068: grouped by installation account)


@router.get("/repositories")
def repositories(request: Request, saved: int = 0, error: str = ""):
    from ..models import Installation, Repository

    with session_scope(read_only=True) as sess:
        installations = sess.query(Installation).filter(Installation.deleted_at.is_(None)).all()
        repos = sess.query(Repository).filter(Repository.deleted_at.is_(None)).all()
        groups = []
        for inst in installations:
            group_repos = [r for r in repos if r.installation_id == inst.id]
            groups.append({"installation": inst, "repositories": group_repos})
        orphan = [r for r in repos if all(r.installation_id != i.id for i in installations)]
        if orphan:
            groups.append({"installation": None, "repositories": orphan})
    return _t(request).TemplateResponse(
        request,
        "repositories.html",
        base_context(request, groups=groups, saved=bool(saved), error=error, branches_of=_branches_of),
    )


def _branches_of(repo):
    from ..core.jobs import configured_branches

    return configured_branches(repo)


# ---------------------------------------------------------------------------
# P4 — entry composer + detail


@router.get("/entries/new")
def entry_new(request: Request):
    with session_scope(read_only=True) as sess:
        default_language = config_store.default_language(sess)
    return _t(request).TemplateResponse(
        request,
        "entry_form.html",
        base_context(
            request,
            errors={},
            values={},
            languages=installed_pack_names(),
            default_language=default_language,
        ),
    )


@router.get("/entries/{entry_id}")
def entry_detail(request: Request, entry_id: int):
    from ..models import ChangelogEntry, DerivedPattern, FeedbackFlag

    with session_scope(read_only=True) as sess:
        entry = sess.get(ChangelogEntry, entry_id)
        if entry is None:
            return _or_404(request, "Entry not found")
        pattern = (
            sess.query(DerivedPattern)
            .filter_by(entry_id=entry_id)
            .order_by(DerivedPattern.id.desc())
            .first()
        )
        from ..models import Occurrence, ScanJob

        flags = (
            sess.query(FeedbackFlag)
            .join(Occurrence, FeedbackFlag.occurrence_id == Occurrence.id)
            .join(ScanJob, Occurrence.scan_job_id == ScanJob.id)
            .filter(ScanJob.entry_id == entry_id, FeedbackFlag.deleted_at.is_(None))
            .order_by(FeedbackFlag.id.desc())
            .all()
        )
        pattern_json = json.loads(pattern.pattern_json) if pattern else None
    return _t(request).TemplateResponse(
        request,
        "entry_detail.html",
        base_context(request, entry=entry, pattern=pattern, pattern_json=pattern_json, flags=flags),
    )


# ---------------------------------------------------------------------------
# P5 — scan console (with htmx polling partial)


def _scan_cards_data(sess, entry_id: int):
    from ..models import Occurrence, Repository, ScanJob

    jobs_for_entry = (
        sess.query(ScanJob)
        .filter(ScanJob.entry_id == entry_id, ScanJob.deleted_at.is_(None))
        .order_by(ScanJob.id.desc())
        .all()
    )
    cards = {}
    for job in jobs_for_entry:
        card = cards.setdefault(
            job.repository_id,
            {"repo": sess.get(Repository, job.repository_id), "branches": [], "default_jobs": []},
        )
        occurrences = []
        if job.status == "done" and job.occurrences_count:
            occurrences = (
                sess.query(Occurrence)
                .filter(Occurrence.scan_job_id == job.id, Occurrence.deleted_at.is_(None))
                .all()
            )
        card["branches"].append(
            {
                "job": job,
                "is_default": card["repo"] is not None and job.branch == card["repo"].default_branch,
                "occurrences": occurrences,
            }
        )
    return list(cards.values())


@router.get("/entries/{entry_id}/scan")
def scan_console(request: Request, entry_id: int):
    from ..models import ChangelogEntry

    with session_scope(read_only=True) as sess:
        entry = sess.get(ChangelogEntry, entry_id)
        if entry is None:
            return _or_404(request, "Entry not found")
        cards = _scan_cards_data(sess, entry_id)
        engine_failures = config_store.engine_consecutive_failures(sess)
    return _t(request).TemplateResponse(
        request,
        "scan_console.html",
        base_context(request, entry=entry, cards=cards, engine_failures=engine_failures),
    )


@router.get("/entries/{entry_id}/scan/cards", response_class=HTMLResponse)
def scan_cards_partial(request: Request, entry_id: int):
    with session_scope(read_only=True) as sess:
        cards = _scan_cards_data(sess, entry_id)
        engine_failures = config_store.engine_consecutive_failures(sess)
    return _t(request).TemplateResponse(
        request,
        "scan_cards.html",
        base_context(request, cards=cards, engine_failures=engine_failures),
    )


# ---------------------------------------------------------------------------
# P6 — diff review


def _latest_remediation(sess, entry_id: int, repository_id: int):
    from ..models import Remediation

    return (
        sess.query(Remediation)
        .filter(Remediation.entry_id == entry_id, Remediation.repository_id == repository_id, Remediation.deleted_at.is_(None))
        .order_by(Remediation.id.desc())
        .first()
    )


@router.get("/entries/{entry_id}/repos/{rid}/review")
def review(request: Request, entry_id: int, rid: int):
    from ..models import ChangelogEntry, Confirmation, Repository

    with session_scope(read_only=True) as sess:
        entry = sess.get(ChangelogEntry, entry_id)
        repo = sess.get(Repository, rid)
        if entry is None or repo is None:
            return _or_404(request, "Entry or repository not found")
        rem = _latest_remediation(sess, entry_id, rid)
        provenance = None
        confirmed = False
        stale = False
        if rem is not None:
            if rem.provenance_json:
                try:
                    provenance = Provenance.from_json_dict(json.loads(rem.provenance_json))
                except (ValueError, TypeError):
                    provenance = None
            match = (
                sess.query(Confirmation)
                .filter_by(remediation_id=rem.id, diff_sha256=rem.diff_sha256)
                .first()
            )
            confirmed = match is not None
            # AC-022: the stale warning appears only when an approval exists
            # for an OLDER diff of this (entry, repo) pair (i.e. the vendor
            # regenerated after approving).
            stale = (
                rem.status == "ready"
                and not confirmed
                and rem.diff_sha256 is not None
                and _has_any_confirmation(sess, entry_id, rid)
            )
    diff_files = _render_diff_files(rem.diff_content) if rem is not None and rem.diff_content else []
    return _t(request).TemplateResponse(
        request,
        "review.html",
        base_context(
            request,
            entry=entry,
            repo=repo,
            remediation=rem,
            provenance=provenance,
            confirmed=confirmed,
            stale_confirmation=stale,
            dispatch_disabled=config.demo_mode(),
            diff_files=diff_files,
        ),
    )


def _render_diff_files(diff_text: str):
    """FR-018: per-file collapsible diff with +/- syntax highlighting."""
    from ..core.diffapply import per_file_diffs

    files = []
    for path, file_text in per_file_diffs(diff_text).items():
        lines = []
        for line in file_text.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                kind = "hdr"
            elif line.startswith("@@"):
                kind = "hunk"
            elif line.startswith("+"):
                kind = "add"
            elif line.startswith("-"):
                kind = "del"
            else:
                kind = "ctx"
            lines.append({"text": line, "kind": kind})
        files.append({"path": path, "lines": lines})
    return files


def _has_any_confirmation(sess, entry_id: int, repository_id: int) -> bool:
    from ..models import Confirmation

    return sess.query(Confirmation).filter_by(entry_id=entry_id, repository_id=repository_id).first() is not None


@router.get("/entries/{entry_id}/repos/{rid}/review/diff.patch")
def download_patch(request: Request, entry_id: int, rid: int):
    """FR-067: diff-export download (dispatch_failed fallback)."""
    with session_scope(read_only=True) as sess:
        rem = _latest_remediation(sess, entry_id, rid)
        if rem is None or not rem.diff_content:
            return _or_404(request, "No remediation diff available")
        filename = f"patchline-entry-{entry_id}-repo-{rid}.patch"
        return Response(
            content=rem.diff_content,
            media_type="text/x-patch",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


# ---------------------------------------------------------------------------
# P7 — pull requests


@router.get("/pull-requests")
def pull_requests(request: Request, synced: int = 0):
    from ..models import ChangelogEntry, CoverageState, PullRequest, Repository

    with session_scope(read_only=True) as sess:
        rows = sess.query(PullRequest).filter(PullRequest.deleted_at.is_(None)).order_by(PullRequest.id.desc()).all()
        items = []
        manual_repo_entries = {
            (r.entry_id, r.repository_id)
            for r in sess.query(CoverageState).filter_by(manual_migrated=True).all()
        }
        for row in rows:
            repo = sess.get(Repository, row.repository_id)
            entry = sess.get(ChangelogEntry, row.entry_id)
            items.append(
                {
                    "row": row,
                    "repo_full_name": repo.full_name if repo else "unknown",
                    "entry_title": entry.title if entry else "unknown",
                    "manual": (row.entry_id, row.repository_id) in manual_repo_entries or row.manual,
                }
            )
    return _t(request).TemplateResponse(
        request,
        "pull_requests.html",
        base_context(request, items=items, synced=bool(synced)),
    )


# ---------------------------------------------------------------------------
# P9 — jobs console


@router.get("/jobs")
def jobs_console(request: Request):
    from ..models import Repository, ScanJob

    with session_scope(read_only=True) as sess:
        jobs = (
            sess.query(ScanJob)
            .filter(ScanJob.deleted_at.is_(None))
            .order_by(ScanJob.id.desc())
            .limit(200)
            .all()
        )
        items = [
            {
                "job": job,
                "repo_full_name": (sess.get(Repository, job.repository_id).full_name if sess.get(Repository, job.repository_id) else "unknown"),
            }
            for job in jobs
        ]
        failures = config_store.engine_consecutive_failures(sess)
    heartbeat = _heartbeat_age()
    return _t(request).TemplateResponse(
        request,
        "jobs.html",
        base_context(request, items=items, engine_failures=failures, heartbeat_age=heartbeat),
    )


def _heartbeat_age():
    path = config.worker_heartbeat_path()
    try:
        return max(0, int(time.time() - os.path.getmtime(path)))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# P10 — settings (operator-facing; Sneferu brand shown here, CP-10)


@router.get("/settings")
def settings(request: Request):
    engine = request.app.state.engine
    version = None
    try:
        version = engine.get_version()
    except Exception:  # noqa: BLE001 - get_version()=None → 'unavailable' display
        version = None
    with session_scope(read_only=True) as sess:
        default_language = config_store.default_language(sess)
        instance_id = config_store.instance_id(sess)
        from ..models import DerivedPattern, WebhookDelivery

        run_ids = [
            row.engine_run_id
            for row in sess.query(DerivedPattern)
            .filter(DerivedPattern.engine_run_id.isnot(None))
            .order_by(DerivedPattern.id.desc())
            .limit(10)
            .all()
        ]
        deliveries_total = sess.query(WebhookDelivery).count()
        deliveries_failed = sess.query(WebhookDelivery).filter_by(status="failed").count()
    notification_status = "configured" if config.notification_webhook_url() else ("unpaired" if config.notification_signing_secret() else "disabled")
    return _t(request).TemplateResponse(
        request,
        "settings.html",
        base_context(
            request,
            engine_mode=("mock" if config.demo_mode() else config.engine_mode()),
            engine_version=version,
            languages=installed_pack_names(),
            default_language=default_language,
            run_ids=run_ids,
            deliveries_total=deliveries_total,
            deliveries_failed=deliveries_failed,
            notification_status=notification_status,
            instance_id=instance_id,
            app_version=APP_VERSION,
        ),
    )


# ---------------------------------------------------------------------------
# FR-074 — /metrics (operator-facing JSON, session required)


@router.get("/metrics")
def metrics(request: Request):
    from ..models import ScanJob

    with session_scope(read_only=True) as sess:
        queue_depth = sess.query(ScanJob).filter_by(status="queued").count()
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        errors = (
            sess.query(ScanJob)
            .filter(ScanJob.status == "failed", ScanJob.completed_at >= one_hour_ago)
            .count()
        )
        p95 = config_store.engine_latency_p95(sess)
    db_path = config.database_url().replace("sqlite:///", "")
    data_dir = os.path.dirname(db_path) or "."
    try:
        usage = shutil.disk_usage(data_dir)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        disk = {"total": None, "used": None, "free": None}
    return JSONResponse(
        {
            "queue_depth": queue_depth,
            "engine_latency_p95_seconds": p95,
            "errors_last_hour": errors,
            "data_partition": data_dir,
            "disk": disk,
        }
    )
