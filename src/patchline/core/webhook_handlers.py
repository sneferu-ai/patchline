"""GitHub webhook processing (FR-005/FR-006/FR-025/FR-035/FR-036/FR-062).

Structure per FR-005: external GitHub API calls happen FIRST, outside any
database transaction; then a SINGLE transaction (1) upserts the delivery row
as ``processing``, (2) performs all side effects as idempotent upserts,
(3) marks the delivery ``succeeded``. A mid-transaction crash rolls back
everything including the delivery row, so GitHub's retry is processed
cleanly. A delivery recorded ``succeeded`` short-circuits to a 200 skip.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..db import session_scope
from . import config_store, jobs, notifications

log = logging.getLogger("patchline.webhooks")

VERIFICATION_DELAY_SECONDS = 5  # FR-024: allow GitHub ref propagation


class WebhookProcessingError(Exception):
    pass


def delivery_already_succeeded(delivery_id: str) -> bool:
    from ..models import WebhookDelivery

    with session_scope() as sess:
        row = sess.query(WebhookDelivery).filter_by(github_delivery_id=delivery_id).one_or_none()
        return bool(row and row.status == "succeeded")


def process_webhook(delivery_id: str, event_type: str, payload: Dict[str, Any], scm) -> str:
    """Dispatch one verified webhook. Returns 'processed' or 'skipped'."""
    if delivery_already_succeeded(delivery_id):
        return "skipped"

    # ---- external calls first (outside any transaction) -------------------
    handler = _HANDLERS.get(event_type)
    if handler is None:
        log.info("webhook %s: no handler for event %r; recording as succeeded", delivery_id, event_type)
    external = None
    if handler is not None:
        external = handler.prepare(payload, scm)

    # ---- single transaction ------------------------------------------------
    from ..models import WebhookDelivery

    with session_scope() as sess:
        delivery = sess.query(WebhookDelivery).filter_by(github_delivery_id=delivery_id).one_or_none()
        if delivery is None:
            delivery = WebhookDelivery(github_delivery_id=delivery_id, event_type=event_type, status="processing")
            sess.add(delivery)
            sess.flush()
        else:
            delivery.status = "processing"  # stale 'processing'/'failed' rows reprocess (FR-035)
        if handler is not None:
            handler.apply(sess, payload, external)
        delivery.status = "succeeded"
        delivery.processed_at = datetime.now(timezone.utc)
    # Deliver queued notifications AFTER the single transaction commits (CP-2).
    notifications.flush_pending()
    return "processed"


# ---------------------------------------------------------------------------
# Event handlers: prepare() = external I/O; apply() = single-tx side effects.


class _InstallationHandler:
    """installation.created / deleted / suspended (FR-006)."""

    @staticmethod
    def prepare(payload: Dict[str, Any], scm) -> Dict[str, Any]:
        installation = payload.get("installation") or {}
        installation_id = installation.get("id")
        repos = []
        action = payload.get("action")
        if installation_id and action in ("created", "new_permissions_accepted"):
            try:
                repos = scm.list_installation_repositories(installation_id)
            except Exception as exc:  # noqa: BLE001 - webhook payload repos may still be usable
                log.warning("installation %s repository fetch failed: %s", installation_id, exc)
                repos = [
                    {"full_name": r.get("full_name"), "default_branch": (r.get("default_branch") or "main"), "primary_language": r.get("language")}
                    for r in payload.get("repositories") or []
                    if r.get("full_name")
                ]
        account = installation.get("account") or {}
        return {
            "installation_id": installation_id,
            "account_login": account.get("login") or "unknown",
            "account_type": account.get("type") or "User",
            "action": action,
            "repositories": repos,
        }

    @staticmethod
    def apply(sess, payload: Dict[str, Any], external: Dict[str, Any]) -> None:
        from ..models import Installation

        action = external["action"]
        inst = (
            sess.query(Installation)
            .filter_by(github_installation_id=external["installation_id"])
            .one_or_none()
        )
        if inst is None:
            inst = Installation(
                github_installation_id=external["installation_id"],
                account_login=external["account_login"],
                account_type=external["account_type"],
                connection_state="active",
            )
            sess.add(inst)
            sess.flush()
        inst.account_login = external["account_login"]
        if action == "deleted":
            inst.connection_state = "removed"
        elif action == "suspended":
            inst.connection_state = "suspended"
        elif action in ("created", "new_permissions_accepted", "unsuspended"):
            inst.connection_state = "active"
        inst.updated_at = datetime.now(timezone.utc)
        for repo_data in external["repositories"]:
            _upsert_repository(sess, inst, repo_data)
        if action in ("created", "new_permissions_accepted"):
            _auto_scan_ready_entries(sess)


class _InstallationRepositoriesHandler:
    """installation_repositories added/removed (FR-006/FR-062)."""

    @staticmethod
    def prepare(payload: Dict[str, Any], scm) -> Dict[str, Any]:
        installation = payload.get("installation") or {}
        return {
            "installation_id": installation.get("id"),
            "account_login": (installation.get("account") or {}).get("login") or "unknown",
            "account_type": (installation.get("account") or {}).get("type") or "User",
            "action": payload.get("action"),
            "added": payload.get("repositories_added") or [],
            "removed": payload.get("repositories_removed") or [],
        }

    @staticmethod
    def apply(sess, payload: Dict[str, Any], external: Dict[str, Any]) -> None:
        from ..models import Installation, Repository

        inst = (
            sess.query(Installation)
            .filter_by(github_installation_id=external["installation_id"])
            .one_or_none()
        )
        if inst is None:
            inst = Installation(
                github_installation_id=external["installation_id"],
                account_login=external["account_login"],
                account_type=external["account_type"],
                connection_state="active",
            )
            sess.add(inst)
            sess.flush()
        for repo_data in external["added"]:
            _upsert_repository(
                sess,
                inst,
                {
                    "full_name": repo_data.get("full_name"),
                    "default_branch": repo_data.get("default_branch") or "main",
                    "primary_language": repo_data.get("language"),
                },
            )
        for repo_data in external["removed"]:
            repo = sess.query(Repository).filter_by(full_name=repo_data.get("full_name")).one_or_none()
            if repo is not None:
                repo.connection_state = "removed"
        if external["added"]:
            _auto_scan_ready_entries(sess)  # FR-062


class _PullRequestHandler:
    """pull_request opened/closed (FR-025/FR-036/FR-024)."""

    @staticmethod
    def prepare(payload: Dict[str, Any], scm) -> Dict[str, Any]:
        return {}

    @staticmethod
    def apply(sess, payload: Dict[str, Any], external: Dict[str, Any]) -> None:
        from ..models import PullRequest
        from .coverage import apply_transition

        action = payload.get("action")
        pr = payload.get("pull_request") or {}
        repo_full = (payload.get("repository") or {}).get("full_name")
        number = pr.get("number")
        if not repo_full or number is None:
            return
        from ..models import Repository

        repo = sess.query(Repository).filter_by(full_name=repo_full).one_or_none()
        if repo is None:
            return
        row = (
            sess.query(PullRequest)
            .filter_by(repository_id=repo.id, pr_number=number)
            .order_by(PullRequest.id.desc())
            .first()
        )
        if row is None:
            return  # not a Patchline-opened PR
        if action == "closed":
            if pr.get("merged"):
                row.state = "merged"
                row.updated_at = datetime.now(timezone.utc)
                entry = _entry_for(sess, row.entry_id)
                if entry is not None:
                    # FR-024: single verification scan after a 5s propagation
                    # delay; coverage stays pr_open until the result lands.
                    jobs.enqueue_verification(sess, entry, repo, delay_seconds=VERIFICATION_DELAY_SECONDS)
                notifications.record_and_deliver(
                    sess,
                    "pr_merged",
                    instance_id=config_store.instance_id(sess),
                    entry_id=row.entry_id,
                    repository_name=repo_full,
                    status=f"PR #{number} merged",
                )
            else:
                row.state = "rejected"
                row.updated_at = datetime.now(timezone.utc)
                apply_transition(
                    sess,
                    entry_id=row.entry_id,
                    repository_id=repo.id,
                    to_status="pr_rejected",
                    job_type="webhook",
                    detail=f"PR #{number} closed without merge",
                )
                notifications.record_and_deliver(
                    sess,
                    "pr_rejected",
                    instance_id=config_store.instance_id(sess),
                    entry_id=row.entry_id,
                    repository_name=repo_full,
                    status=f"PR #{number} rejected",
                )
        elif action in ("opened", "reopened", "synchronize"):
            row.state = "open"
            row.updated_at = datetime.now(timezone.utc)


def _entry_for(sess, entry_id: int):
    from ..models import ChangelogEntry

    return sess.get(ChangelogEntry, entry_id)


def _upsert_repository(sess, inst, repo_data: Dict[str, Any]) -> None:
    from ..models import Repository

    full_name = repo_data.get("full_name")
    if not full_name:
        return
    repo = sess.query(Repository).filter_by(full_name=full_name).one_or_none()
    if repo is None:
        repo = Repository(
            installation_id=inst.id,
            full_name=full_name,
            default_branch=repo_data.get("default_branch") or "main",
            primary_language=(repo_data.get("primary_language") or "").lower() or None,
            connection_state="active",
        )
        sess.add(repo)
        sess.flush()
    else:
        repo.installation_id = inst.id
        repo.connection_state = "active"
        if repo_data.get("default_branch"):
            repo.default_branch = repo_data["default_branch"]
        if repo_data.get("primary_language"):
            repo.primary_language = str(repo_data["primary_language"]).lower()


def _auto_scan_ready_entries(sess) -> None:
    """FR-062: enqueue scans for all ready entries against active repos that
    have no queued/running job for the entry."""
    from ..models import ChangelogEntry, Repository, ScanJob

    ready = sess.query(ChangelogEntry).filter_by(status="ready", deleted_at=None).all()
    if not ready:
        return
    active_repos = sess.query(Repository).filter_by(connection_state="active", deleted_at=None).all()
    for entry in ready:
        pending_repo_ids = {
            r
            for (r,) in sess.query(ScanJob.repository_id)
            .filter(ScanJob.entry_id == entry.id, ScanJob.status.in_(["queued", "running"]))
            .all()
        }
        targets = [repo for repo in active_repos if repo.id not in pending_repo_ids]
        if targets:
            jobs.enqueue_scans_for_entry(sess, entry, targets, priority=jobs.PRIORITY_USER)


_HANDLERS = {
    "installation": _InstallationHandler,
    "installation_repositories": _InstallationRepositoriesHandler,
    "pull_request": _PullRequestHandler,
}
