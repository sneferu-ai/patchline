"""FR-008 re-check installations: reconcile DB against the GitHub API.

Shared by the P2 action and demo-mode login (so the demo board is populated
from fixtures without a manual click).
"""
from __future__ import annotations

import logging

from ..db import session_scope
from .webhook_handlers import _upsert_repository

log = logging.getLogger("patchline.recheck")


def recheck_installations(scm) -> int:
    """Returns the number of installations reconciled. Raises on API failure."""
    from ..models import Installation, Repository

    gh_installations = scm.list_installations()
    with session_scope() as sess:
        seen_ids = set()
        for gh_inst in gh_installations:
            gh_id = gh_inst.get("id") or gh_inst.get("installation_id")
            if gh_id is None:
                continue
            seen_ids.add(int(gh_id))
            account = gh_inst.get("account") or {}
            account_login = gh_inst.get("account_login") or account.get("login") or "unknown"
            account_type = gh_inst.get("account_type") or account.get("type") or "User"
            inst = sess.query(Installation).filter_by(github_installation_id=int(gh_id)).one_or_none()
            if inst is None:
                inst = Installation(
                    github_installation_id=int(gh_id),
                    account_login=account_login,
                    account_type=account_type,
                    connection_state="active",
                )
                sess.add(inst)
                sess.flush()
            inst.connection_state = "active"
            try:
                repos = scm.list_installation_repositories(int(gh_id))
            except Exception as exc:  # noqa: BLE001
                log.warning("repository listing failed for installation %s: %s", gh_id, exc)
                repos = []
            for repo_data in repos:
                _upsert_repository(sess, inst, repo_data)
        # Repositories uninstalled on GitHub are marked removed (FR-008).
        for inst in sess.query(Installation).all():
            if inst.github_installation_id not in seen_ids:
                inst.connection_state = "removed"
                for repo in sess.query(Repository).filter_by(installation_id=inst.id).all():
                    repo.connection_state = "removed"
    return len(seen_ids)
