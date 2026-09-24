"""State-changing POST routes (all CSRF-guarded, FR-031)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import auth, config
from ..adapters.languages import is_installed
from ..core import dispatch as dispatch_flow
from ..core import jobs as job_ops
from ..core.security import safe_redirect_target, validate_branch_names
from ..core.slugify import slugify_title
from ..db import session_scope
from ..webutil import base_context, csrf_ok, form_data

log = logging.getLogger("patchline.routes.actions")

router = APIRouter()


def _forbidden(request: Request):
    return JSONResponse({"error": "missing or invalid CSRF token"}, status_code=403)


def _t(request: Request):
    return request.app.state.templates


# ---------------------------------------------------------------------------
# P4 — create entry (FR-009/FR-026/FR-073)


def _validate_entry_form(form: dict) -> dict:
    """Per-field validation; the errors dict names ONLY violated fields (AC-005)."""
    errors = {}
    title = (form.get("title") or "").strip()
    if not (1 <= len(title) <= 200):
        errors["title"] = "title must be 1–200 characters"
    prior = (form.get("prior_api_shape") or "").strip()
    if not (1 <= len(prior) <= 5000):
        errors["prior_api_shape"] = "prior_api_shape must be 1–5000 characters"
    target = (form.get("target_contract") or "").strip()
    if not (1 <= len(target) <= 5000):
        errors["target_contract"] = "target_contract must be 1–5000 characters"
    language = (form.get("language") or "").strip().lower()
    if not language or not is_installed(language):
        errors["language"] = "language must be one of the installed language packs"
    return errors


@router.post("/entries/new")
async def entry_create(request: Request):
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    from ..models import ChangelogEntry

    errors = _validate_entry_form(form)
    if errors:
        with session_scope(read_only=True) as sess:
            from ..core import config_store

            default_language = config_store.default_language(sess)
        from ..adapters.languages import installed_pack_names

        return _t(request).TemplateResponse(
            request,
            "entry_form.html",
            base_context(
                request,
                errors=errors,
                values=form,
                languages=installed_pack_names(),
                default_language=default_language,
            ),
            status_code=422,
        )
    with session_scope() as sess:
        vendor = request.state.vendor
        entry = ChangelogEntry(
            vendor_id=vendor.id,
            title=form["title"].strip(),
            prior_api_shape=form["prior_api_shape"].strip(),
            target_contract=form["target_contract"].strip(),
            language=form["language"].strip().lower(),
            status="draft",
            slug=slugify_title(form["title"]) or None,
        )
        sess.add(entry)
        sess.flush()
        if not entry.slug:
            # FR-073: empty-slug fallback requires the auto-incremented id.
            entry.slug = f"entry-{entry.id}"
        entry_id = entry.id
    return RedirectResponse(url=f"/entries/{entry_id}", status_code=302)


# ---------------------------------------------------------------------------
# P5 — scan triggers (FR-012/FR-037)


@router.post("/entries/{entry_id}/scan")
async def scan_trigger(request: Request, entry_id: int):
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    from ..models import ChangelogEntry, Repository

    with session_scope() as sess:
        entry = sess.get(ChangelogEntry, entry_id)
        if entry is None:
            return JSONResponse({"error": "entry not found"}, status_code=404)
        if entry.status != "ready":
            return JSONResponse({"error": f"entry is {entry.status}; only ready entries can be scanned"}, status_code=409)
        repos = sess.query(Repository).filter(Repository.deleted_at.is_(None)).all()
        job_ops.enqueue_scans_for_entry(sess, entry, repos, priority=job_ops.PRIORITY_USER)
    return RedirectResponse(url=f"/entries/{entry_id}/scan", status_code=302)


@router.post("/entries/{entry_id}/repos/{rid}/rescan")
async def rescan_repo(request: Request, entry_id: int, rid: int):
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    from ..models import ChangelogEntry, Repository

    with session_scope() as sess:
        entry = sess.get(ChangelogEntry, entry_id)
        repo = sess.get(Repository, rid)
        if entry is None or repo is None:
            return JSONResponse({"error": "entry or repository not found"}, status_code=404)
        if repo.connection_state != "active":
            return JSONResponse({"error": "repository is not active"}, status_code=409)
        job_ops.enqueue_rescan(sess, entry, repo, priority=job_ops.PRIORITY_USER)
    return RedirectResponse(url=f"/entries/{entry_id}/scan", status_code=302)


# ---------------------------------------------------------------------------
# P5 — feedback flags (FR-039)


@router.post("/occurrences/{oid}/flag")
async def flag_occurrence(request: Request, oid: int):
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    from ..models import FeedbackFlag, Occurrence, ScanJob

    with session_scope() as sess:
        occurrence = sess.get(Occurrence, oid)
        if occurrence is None:
            return JSONResponse({"error": "occurrence not found"}, status_code=404)
        existing = (
            sess.query(FeedbackFlag)
            .filter_by(occurrence_id=oid, vendor_id=request.state.vendor.id, deleted_at=None)
            .first()
        )
        if existing is None:
            sess.add(
                FeedbackFlag(
                    occurrence_id=oid,
                    vendor_id=request.state.vendor.id,
                    reason=(form.get("reason") or "false positive")[:500],
                )
            )
        else:
            existing.deleted_at = datetime.now(timezone.utc)  # toggle off
        entry_id = sess.get(ScanJob, occurrence.scan_job_id).entry_id
    return RedirectResponse(url=f"/entries/{entry_id}/scan", status_code=302)


# ---------------------------------------------------------------------------
# P6 — remediation generation / approval / dispatch / mark-migrated


@router.post("/entries/{entry_id}/repos/{rid}/generate")
async def generate_remediation(request: Request, entry_id: int, rid: int):
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    from ..core.remediation_flow import enqueue_remediation
    from ..models import ChangelogEntry, Repository, ScanJob

    with session_scope() as sess:
        entry = sess.get(ChangelogEntry, entry_id)
        repo = sess.get(Repository, rid)
        if entry is None or repo is None:
            return JSONResponse({"error": "entry or repository not found"}, status_code=404)
        if repo.connection_state != "active":
            return JSONResponse({"error": "repository is not active"}, status_code=409)
        # FR-016: remediation operates on default-branch occurrences only.
        scan_job = (
            sess.query(ScanJob)
            .filter_by(entry_id=entry_id, repository_id=rid, branch=repo.default_branch, status="done")
            .order_by(ScanJob.id.desc())
            .first()
        )
        if scan_job is None or not scan_job.occurrences_count:
            return JSONResponse({"error": "no default-branch occurrences to remediate"}, status_code=409)
        enqueue_remediation(sess, entry_id, rid, scan_job.id)
    return RedirectResponse(url=f"/entries/{entry_id}/repos/{rid}/review", status_code=302)


@router.post("/entries/{entry_id}/repos/{rid}/approve")
async def approve_and_dispatch(request: Request, entry_id: int, rid: int):
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    session = request.state.session
    vendor = request.state.vendor

    # FR-050: step-up before dispatch (demo mode: no-op, FR-060h).
    if auth.needs_step_up(session):
        target = safe_redirect_target(f"/entries/{entry_id}/repos/{rid}/review")
        return RedirectResponse(url=f"/login/start?step_up=1&redirect_to={target}", status_code=302)

    from .surfaces import _latest_remediation

    with session_scope() as sess:
        rem = _latest_remediation(sess, entry_id, rid)
        if rem is None or rem.status not in ("ready", "dispatch_failed"):
            return JSONResponse({"error": "no ready remediation to approve"}, status_code=409)
        remediation_id = rem.id

    if config.demo_mode():
        # FR-060d: dispatch disabled — simulated success state only.
        return RedirectResponse(url=f"/entries/{entry_id}/repos/{rid}/review?simulated=1", status_code=302)

    with session_scope() as sess:
        from ..models import Remediation

        rem = sess.get(Remediation, remediation_id)
        dispatch_flow.record_confirmation(sess, remediation=rem, vendor_id=vendor.id)

    scm = request.app.state.get_scm()
    try:
        result = dispatch_flow.dispatch_remediation(remediation_id, scm, vendor.id)
    except dispatch_flow.StaleConfirmation:
        # FR-020: zero GitHub writes happened.
        return JSONResponse({"error": "stale confirmation: the remediation changed after approval"}, status_code=409)
    except dispatch_flow.DispatchFailure as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    if result == "dispatch_failed":
        return RedirectResponse(url=f"/entries/{entry_id}/repos/{rid}/review?dispatch=failed", status_code=302)
    return RedirectResponse(url="/pull-requests", status_code=302)


@router.post("/entries/{entry_id}/repos/{rid}/mark-migrated")
async def mark_migrated(request: Request, entry_id: int, rid: int):
    """FR-067: manual mark-migrated after the confirmation dialog (client-side)."""
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    if form.get("confirm") != "yes":
        return JSONResponse({"error": "confirmation required"}, status_code=409)
    from ..core.coverage import InvalidTransition

    with session_scope() as sess:
        try:
            dispatch_flow.mark_migrated_manually(sess, entry_id=entry_id, repository_id=rid)
        except InvalidTransition as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    return RedirectResponse(url=f"/entries/{entry_id}/coverage", status_code=302)


# ---------------------------------------------------------------------------
# P3 — branch configuration (FR-040)


@router.post("/repositories/{rid}/branches")
async def configure_branches(request: Request, rid: int):
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    from ..models import Repository

    raw = form.get("branches") or ""
    branches = [b.strip() for b in raw.split(",") if b.strip()]
    errors = validate_branch_names(branches)
    if errors:
        return RedirectResponse(
            url=f"/repositories?error={errors[0].replace(' ', '+')}", status_code=302
        )
    with session_scope() as sess:
        repo = sess.get(Repository, rid)
        if repo is None:
            return JSONResponse({"error": "repository not found"}, status_code=404)
        if repo.connection_state != "active":
            return RedirectResponse(url="/repositories?error=repository+is+read-only", status_code=302)
        full_name = repo.full_name
        default_branch = repo.default_branch
        scm = request.app.state.get_scm()
        for name in branches:
            try:
                exists = scm.branch_exists(full_name, name)
            except Exception as exc:  # noqa: BLE001
                log.warning("branch existence check failed for %s on %s: %s", name, full_name, exc)
                return RedirectResponse(
                    url="/repositories?error=branch+validation+unavailable+right+now", status_code=302
                )
            if not exists:
                return RedirectResponse(
                    url=f"/repositories?error=branch+{name}+does+not+exist+on+{full_name.replace('/', '%2F')}",
                    status_code=302,
                )
        ordered = sorted(set(branches), key=lambda b: (b != default_branch, branches.index(b)))
        repo.scan_branches = json.dumps(ordered)
    return RedirectResponse(url="/repositories?saved=1", status_code=302)


# ---------------------------------------------------------------------------
# P2 — re-check installations (FR-008)


@router.post("/connections/recheck")
async def recheck_installations(request: Request):
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    from ..core.recheck import recheck_installations as _recheck

    scm = request.app.state.get_scm()
    try:
        _recheck(scm)
    except Exception as exc:  # noqa: BLE001
        log.warning("re-check installations failed: %s", exc)
        return RedirectResponse(url="/connections/setup?error=recheck+failed", status_code=302)
    return RedirectResponse(url="/connections/setup?rechecked=1", status_code=302)


# ---------------------------------------------------------------------------
# P7 — sync PR states (FR-025)


@router.post("/pull-requests/sync")
async def sync_pull_requests(request: Request):
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    from ..core import pr_sync

    scm = request.app.state.get_scm()
    try:
        pr_sync.sync_pull_requests(scm)
    except Exception as exc:  # noqa: BLE001
        log.warning("pull-request sync failed: %s", exc)
        return RedirectResponse(url="/pull-requests?synced=0", status_code=302)
    return RedirectResponse(url="/pull-requests?synced=1", status_code=302)


# ---------------------------------------------------------------------------
# P9 — retry a failed job


@router.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: int):
    form = await form_data(request)
    if not csrf_ok(request, form):
        return _forbidden(request)
    from ..models import ScanJob

    with session_scope() as sess:
        job = sess.get(ScanJob, job_id)
        if job is None:
            return JSONResponse({"error": "job not found"}, status_code=404)
        if job.status not in ("failed", "skipped_language"):
            return JSONResponse({"error": f"job is {job.status}, not retryable"}, status_code=409)
        job.status = "queued"
        job.error_message = None
        job.claimed_at = None
        job.started_at = None
        job.next_attempt_at = None
    return RedirectResponse(url="/jobs", status_code=302)
