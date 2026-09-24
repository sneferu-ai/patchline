"""P7 "sync states" poll fallback (FR-025): detect open → merged / rejected
transitions the webhooks missed, and enqueue verification scans for merged
PRs lacking a completed verification.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..db import session_scope
from . import jobs
from .coverage import apply_transition
from .webhook_handlers import VERIFICATION_DELAY_SECONDS

log = logging.getLogger("patchline.pr_sync")


def sync_pull_requests(scm) -> dict:
    """Poll every tracked non-terminal PR. Returns a summary dict."""
    from ..models import ChangelogEntry, PullRequest, Repository

    summary = {"checked": 0, "merged": 0, "rejected": 0, "unchanged": 0, "errors": 0}
    with session_scope() as sess:
        rows = sess.query(PullRequest).filter(PullRequest.state.in_(["open"])).all()
        work = []
        for row in rows:
            repo = sess.get(Repository, row.repository_id)
            if repo is None or row.pr_number is None:
                continue
            work.append((row.id, repo.full_name, row.pr_number))
    for pr_id, repo_full_name, pr_number in work:
        summary["checked"] += 1
        try:
            gh = scm.get_pull_request(repo_full_name, pr_number)
        except Exception as exc:  # noqa: BLE001
            log.warning("sync: fetch PR %s#%s failed: %s", repo_full_name, pr_number, exc)
            summary["errors"] += 1
            continue
        gh_state = (gh or {}).get("state")
        merged_at = (gh or {}).get("merged_at")
        with session_scope() as sess:
            row = sess.get(PullRequest, pr_id)
            if row is None or row.state != "open":
                continue
            repo = sess.get(Repository, row.repository_id)
            entry = sess.get(ChangelogEntry, row.entry_id)
            if gh_state == "closed" and merged_at:
                row.state = "merged"
                row.updated_at = datetime.now(timezone.utc)
                summary["merged"] += 1
                if repo is not None and entry is not None:
                    # FR-025: merged PRs lacking verification get one; the
                    # board stays pr_open until the result lands.
                    jobs.enqueue_verification(sess, entry, repo, delay_seconds=VERIFICATION_DELAY_SECONDS)
            elif gh_state == "closed":
                row.state = "rejected"
                row.updated_at = datetime.now(timezone.utc)
                summary["rejected"] += 1
                apply_transition(
                    sess,
                    entry_id=row.entry_id,
                    repository_id=row.repository_id,
                    to_status="pr_rejected",
                    job_type="pr_sync",
                    detail=f"PR #{pr_number} closed without merge (sync)",
                )
            else:
                summary["unchanged"] += 1
    return summary
