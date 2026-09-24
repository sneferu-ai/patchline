"""PR dispatch (FR-019/FR-020/FR-021) and manual mark-migrated (FR-067).

The confirmation gate is the heart of the trust model: zero GitHub writes
happen without a Confirmation row whose remediation_id AND diff_sha256 match
the current remediation (stale confirmations → 409 Conflict).

Branch naming: ``patchline/{slug}-{YYYYMMDD}-{short-hash}`` (FR-073); on
retry, ``/compare`` decides whether the existing branch already carries the
exact remediation (canonical hash match AND no live PR row) → reuse;
otherwise a new branch with an incremented suffix is created.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from ..adapters.engine import Provenance
from ..adapters.scm.github import FastForwardError, GitHubError, PermissionDeniedError
from ..db import session_scope
from . import config_store, notifications
from .coverage import apply_transition
from .diffapply import apply_unified_diff, per_file_diffs
from .diffhash import canonical_diff_sha256
from .pr_body import compose_pr_body

log = logging.getLogger("patchline.dispatch")


class StaleConfirmation(Exception):
    """409: no confirmation matches the current remediation (FR-020)."""


class DispatchFailure(Exception):
    pass


# ---------------------------------------------------------------------------
# FR-019: approval → confirmation row (append-only audit event)


def record_confirmation(sess, *, remediation, vendor_id: int):
    from ..models import Confirmation

    row = Confirmation(
        remediation_id=remediation.id,
        entry_id=remediation.entry_id,
        repository_id=remediation.repository_id,
        vendor_id=vendor_id,
        diff_sha256=remediation.diff_sha256,
    )
    sess.add(row)
    sess.flush()
    return row


def confirmation_matches(sess, *, remediation) -> bool:
    """FR-020: both remediation_id AND diff_sha256 must match (regeneration
    creates a new Remediation row, so a stale confirmation fails both)."""
    from ..models import Confirmation

    if not remediation.diff_sha256:
        return False
    match = (
        sess.query(Confirmation)
        .filter_by(remediation_id=remediation.id, diff_sha256=remediation.diff_sha256)
        .first()
    )
    return match is not None


# ---------------------------------------------------------------------------
# Branch naming and reuse


def branch_base_name(entry, diff_sha256: str) -> str:
    slug = entry.slug or f"entry-{entry.id}"
    date = entry.created_at.strftime("%Y%m%d") if entry.created_at else datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"patchline/{slug}-{date}-{diff_sha256[:8]}"


def _canonical_compare_hash(compare_payload: dict) -> Optional[str]:
    """Reconstruct a unified-ish diff from a /compare response and hash it
    canonically so it can be compared to the remediation's FR-046 hash."""
    files = (compare_payload or {}).get("files") or []
    if not files:
        return None
    parts = []
    for f in files:
        filename = f.get("filename")
        patch = f.get("patch")
        if not filename or not patch:
            return None  # cannot faithfully reconstruct → treat as no match
        parts.append(f"--- a/{filename}\n+++ b/{filename}\n{patch}")
    return canonical_diff_sha256("\n".join(parts))


def choose_branch(scm, repo_full_name: str, default_branch: str, base_name: str, diff_sha256: str, existing_branches, token: Optional[str]) -> Tuple[str, bool]:
    """FR-021 retry semantics. Returns (branch_name, reuse_existing).

    A branch is reused when its /compare hash matches the remediation's hash
    AND no PullRequest row exists for it outside ``dispatch_failed``.
    ``existing_branches`` maps branch_name → bool has_live_pr (True when a PR
    row exists and is not dispatch_failed).
    """
    for suffix in ["", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9"]:
        candidate = f"{base_name}{suffix}"
        try:
            scm.get_branch_ref(repo_full_name, candidate, token=token)
        except GitHubError:
            return candidate, False  # does not exist → fresh branch
        except Exception:  # noqa: BLE001
            return candidate, False
        has_live_pr = existing_branches.get(candidate, False)
        if has_live_pr:
            continue
        try:
            compare = scm.compare(repo_full_name, default_branch, candidate, token=token)
        except Exception:  # noqa: BLE001
            continue
        compare_hash = _canonical_compare_hash(compare)
        if compare_hash is not None and compare_hash == diff_sha256:
            return candidate, True
    return f"{base_name}-{datetime.now(timezone.utc).strftime('%H%M%S')}", False


# ---------------------------------------------------------------------------
# FR-021: the Git Data API sequence


def dispatch_remediation(remediation_id: int, scm, vendor_id: int) -> str:
    """Gate, then open the PR. Returns 'dispatched' or 'dispatch_failed'.

    Raises StaleConfirmation when the FR-020 gate fails (route → 409, zero
    GitHub writes).
    """
    from ..models import ChangelogEntry, PullRequest, Remediation, Repository

    with session_scope() as sess:
        rem = sess.get(Remediation, remediation_id)
        if rem is None:
            raise DispatchFailure("remediation missing")
        if rem.status not in ("ready", "dispatch_failed"):
            # dispatch_failed is retryable (FR-065: dispatch_failed → pr_open).
            raise DispatchFailure(f"remediation status is {rem.status}, not dispatchable")
        if not confirmation_matches(sess, remediation=rem):
            raise StaleConfirmation("no confirmation matches the current remediation diff hash")
        entry = sess.get(ChangelogEntry, rem.entry_id)
        repo = sess.get(Repository, rem.repository_id)
        if entry is None or repo is None:
            raise DispatchFailure("entry or repository missing")
        if repo.connection_state != "active":
            raise DispatchFailure(f"repository connection_state={repo.connection_state}")
        entry_id, repo_id = entry.id, repo.id
        repo_full_name = repo.full_name
        default_branch = repo.default_branch
        base_name = branch_base_name(entry, rem.diff_sha256)
        diff_text = rem.diff_content or ""
        provenance = Provenance.from_json_dict(_loads(rem.provenance_json)) if rem.provenance_json else None
        pr_title = f"[Patchline] {entry.title}"
        existing = {
            row.branch_name: (row.state != "dispatch_failed")
            for row in sess.query(PullRequest).filter_by(repository_id=repo_id, entry_id=entry_id).all()
            if row.branch_name
        }

    installation_id = _installation_id_for_repo(repo_id)
    archive_dir = None
    try:
        token = scm.mint_installation_token(installation_id) if installation_id else None
        repo_meta = scm.get_repo(repo_full_name, token=token)
        default_branch = repo_meta.get("default_branch") or default_branch
        base_ref = scm.get_branch_ref(repo_full_name, default_branch, token=token)
        base_sha = (base_ref.get("object") or {}).get("sha")
        if not base_sha:
            raise DispatchFailure("could not resolve default branch HEAD sha")

        branch_name, reuse = choose_branch(
            scm, repo_full_name, default_branch, base_name, rem.diff_sha256, existing, token
        )
        if not reuse:
            # Reconstruct each patched file against the real base content so
            # the blobs we commit are the exact post-patch files (FR-017's
            # reconstruction contract applies to dispatch too).
            from .remediation_flow import fetch_archive_dir_for_repo

            archive_dir, _ = fetch_archive_dir_for_repo(scm, repo_full_name, default_branch, base_sha, token, None)
            scm.create_ref(repo_full_name, f"refs/heads/{branch_name}", base_sha, token=token)
            tree_items = []
            for path, content in _patched_file_contents(diff_text, archive_dir):
                blob = scm.create_blob(
                    repo_full_name,
                    base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    token=token,
                )
                tree_items.append(
                    {"path": path, "mode": "100644", "type": "blob", "sha": blob.get("sha")}
                )
            tree = scm.create_tree(repo_full_name, base_sha, tree_items, token=token)
            commit = scm.create_commit(
                repo_full_name,
                f"patchline: migrate to {entry.title}",
                tree.get("sha"),
                [base_sha],
                token=token,
            )
            scm.update_patchline_ref(repo_full_name, branch_name, commit.get("sha"), token=token)
        body = compose_pr_body(
            entry_title=entry.title,
            summary=entry.target_contract[:800],
            provenance=provenance,
        )
        pr = scm.create_pull_request(
            repo_full_name,
            head=branch_name,
            base=default_branch,
            title=pr_title,
            body=body,
            token=token,
        )
    except StaleConfirmation:
        raise
    except FastForwardError as exc:
        _mark_dispatch_failed(remediation_id, repo_id, entry_id, str(exc))
        return "dispatch_failed"
    except PermissionDeniedError as exc:
        _mark_dispatch_failed(remediation_id, repo_id, entry_id, f"permission denied: {exc}")
        return "dispatch_failed"
    except Exception as exc:  # noqa: BLE001 - partial failure: orphan branch left for inspection, diff retained
        log.exception("dispatch of remediation %s failed", remediation_id)
        _mark_dispatch_failed(remediation_id, repo_id, entry_id, str(exc))
        return "dispatch_failed"
    finally:
        if archive_dir:
            import shutil

            shutil.rmtree(archive_dir, ignore_errors=True)

    with session_scope() as sess:
        rem = sess.get(Remediation, remediation_id)
        rem.status = "dispatched"
        rem.updated_at = datetime.now(timezone.utc)
        row = PullRequest(
            remediation_id=remediation_id,
            repository_id=repo_id,
            entry_id=entry_id,
            branch_name=branch_name,
            pr_number=pr.get("number"),
            pr_url=pr.get("html_url"),
            state="open",
        )
        sess.add(row)
        apply_transition(
            sess,
            entry_id=entry_id,
            repository_id=repo_id,
            to_status="pr_open",
            job_type="dispatch",
        )
        notifications.record_and_deliver(
            sess,
            "pr_dispatched",
            instance_id=config_store.instance_id(sess),
            entry_id=entry_id,
            repository_name=repo_full_name,
            status=f"PR #{pr.get('number')}",
        )
    notifications.flush_pending()  # deliver AFTER the write tx commits (CP-2)
    return "dispatched"


def _mark_dispatch_failed(remediation_id: int, repo_id: int, entry_id: int, message: str) -> None:
    from ..models import PullRequest, Remediation

    with session_scope() as sess:
        rem = sess.get(Remediation, remediation_id)
        if rem is not None:
            rem.status = "dispatch_failed"
            rem.error_message = message[:900]
            rem.updated_at = datetime.now(timezone.utc)
        existing = (
            sess.query(PullRequest)
            .filter_by(remediation_id=remediation_id, state="dispatch_failed")
            .first()
        )
        if existing is None:
            sess.add(
                PullRequest(
                    remediation_id=remediation_id,
                    repository_id=repo_id,
                    entry_id=entry_id,
                    state="dispatch_failed",
                )
            )
        apply_transition(
            sess,
            entry_id=entry_id,
            repository_id=repo_id,
            to_status="dispatch_failed",
            job_type="dispatch",
            detail=message[:400],
        )


def _patched_file_contents(diff_text: str, archive_dir: str):
    """(path, new_content) pairs: apply each file's hunks to its real base
    content from the fetched archive (empty base for new files)."""
    import os

    for path, file_diff in per_file_diffs(diff_text).items():
        try:
            with open(os.path.join(archive_dir, path), "r", encoding="utf-8") as fh:
                original = fh.read()
        except OSError:
            original = ""
        try:
            yield path, apply_unified_diff(original, file_diff)
        except Exception as exc:  # noqa: BLE001
            raise DispatchFailure(f"cannot reconstruct {path}: {exc}") from exc


def _loads(raw: str):
    import json

    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _installation_id_for_repo(repo_id: int) -> Optional[int]:
    from .scanning import repo_installation_id

    return repo_installation_id(repo_id)


# ---------------------------------------------------------------------------
# FR-067: manual mark-migrated (diff-export fallback)


def mark_migrated_manually(sess, *, entry_id: int, repository_id: int) -> None:
    """Sets coverage to migrated with manual_migrated=True. Only reachable
    from dispatch_failed / migration_incomplete per FR-065."""
    apply_transition(
        sess,
        entry_id=entry_id,
        repository_id=repository_id,
        to_status="migrated",
        manual_migrated=True,
        job_type="manual",
        detail="marked as migrated manually (diff-export path; no verification scan)",
    )
