"""Fixture-backed GitHub client for DEMO_MODE (FR-060b) and hermetic tests.

Responses come from ``tests/fixtures/github/*.json`` (``fixtures_version``
tracked per file). Every Git Data API call is recorded in ``self.calls`` so
tests can assert the exact dispatch sequence (AC-003 evidence) — and so demo
mode can prove it made no real network calls.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("patchline.github.mock")

FIXTURES_VERSION = "1.0"


def default_github_fixture_dir() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "tests", "fixtures", "github")
    )


class FixtureGitHubClient:
    """Drop-in for GitHubClient in demo mode / tests. Never touches network."""

    def __init__(self, fixture_dir: Optional[str] = None):
        self.fixture_dir = fixture_dir or default_github_fixture_dir()
        self.calls: List[Dict[str, Any]] = []
        self._pr_counter = 100
        self._opened_prs: List[Dict[str, Any]] = []
        self._created_refs: Dict[str, str] = {}  # full_name/branch → sha (created this session)

    # -- fixtures -------------------------------------------------------------

    def _load(self, name: str) -> Dict[str, Any]:
        path = os.path.join(self.fixture_dir, f"{name}.json")
        if not os.path.exists(path):
            return {"fixtures_version": FIXTURES_VERSION}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("github fixture %s unreadable: %s", path, exc)
            return {"fixtures_version": FIXTURES_VERSION}
        version = str(data.get("fixtures_version", ""))
        if version != FIXTURES_VERSION:
            log.warning("github fixture %s has fixtures_version=%r (expected %r)", path, version, FIXTURES_VERSION)
        return data

    def _record(self, method: str, path: str, **extra: Any) -> None:
        self.calls.append({"method": method, "path": path, "at": datetime.now(timezone.utc).isoformat(), **extra})

    # -- app-level --------------------------------------------------------------

    def mint_installation_token(self, installation_id: int) -> str:
        self._record("POST", f"/app/installations/{installation_id}/access_tokens")
        return f"mock-installation-token-{installation_id}"

    def list_installations(self) -> List[Dict[str, Any]]:
        self._record("GET", "/app/installations")
        return list(self._load("installations").get("installations", []))

    def list_installation_repositories(self, installation_id: int) -> List[Dict[str, Any]]:
        self._record("GET", "/installation/repositories", installation_id=installation_id)
        repos = [
            r
            for r in self._load("repositories").get("repositories", [])
            if int(r.get("installation_id", -1)) == int(installation_id)
        ]
        return [dict(r) for r in repos]

    # -- repository metadata ------------------------------------------------------

    def _repo_row(self, full_name: str) -> Optional[Dict[str, Any]]:
        for r in self._load("repositories").get("repositories", []):
            if r.get("full_name") == full_name:
                return r
        return None

    def get_repo(self, full_name: str, token: Optional[str] = None) -> Dict[str, Any]:
        self._record("GET", f"/repos/{full_name}")
        row = self._repo_row(full_name) or {}
        return {
            "full_name": full_name,
            "default_branch": row.get("default_branch", "main"),
            "language": row.get("primary_language", "Python"),
        }

    def branch_exists(self, full_name: str, branch: str, token: Optional[str] = None) -> bool:
        self._record("GET", f"/repos/{full_name}/branches/{branch}")
        row = self._repo_row(full_name) or {}
        known = {row.get("default_branch", "main")}
        known.update(row.get("branches", []) or [])
        return branch in known

    def get_branch_ref(self, full_name: str, branch: str, token: Optional[str] = None) -> Dict[str, Any]:
        self._record("GET", f"/repos/{full_name}/git/refs/heads/{branch}")
        key = f"{full_name}@{branch}"
        if key in self._created_refs:
            return {"ref": f"refs/heads/{branch}", "object": {"sha": self._created_refs[key], "type": "commit"}}
        row = self._repo_row(full_name) or {}
        known = {row.get("default_branch", "main")}
        known.update(row.get("branches", []) or [])
        if branch not in known:
            from .github import GitHubError

            raise GitHubError(f"ref not found: {branch}", status=404)
        sha = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha, "type": "commit"}}

    # -- archive --------------------------------------------------------------------

    def fetch_archive_dir(self, full_name: str, ref: str, dest_parent: Optional[str] = None) -> str:
        """Mock 'archive': a temp directory populated from the repo fixture's
        ``files`` map ({path: content}); a tiny default when absent."""
        self._record("GET", f"/repos/{full_name}/zipball/{ref}")
        dest = tempfile.mkdtemp(prefix="patchline-mock-archive-", dir=dest_parent)
        row = self._repo_row(full_name) or {}
        files = row.get("files") or {"src/example.py": "user = get_user(user_id)\n"}
        for rel, content in files.items():
            path = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(content))
        return dest

    # -- Git Data sequence (recorded; synthetic objects) ----------------------------

    def create_ref(self, full_name: str, ref: str, sha: str, token: Optional[str] = None) -> Dict[str, Any]:
        self._record("POST", f"/repos/{full_name}/git/refs", ref=ref, sha=sha)
        if ref.startswith("refs/heads/"):
            self._created_refs[f"{full_name}@{ref[len('refs/heads/'):]}"] = sha
        return {"ref": ref, "object": {"sha": sha}}

    def create_blob(self, full_name: str, content_b64: str, token: Optional[str] = None) -> Dict[str, Any]:
        sha = hashlib.sha256(content_b64.encode("utf-8")).hexdigest()
        self._record("POST", f"/repos/{full_name}/git/blobs", sha=sha)
        return {"sha": sha}

    def create_tree(self, full_name: str, base_tree: str, tree_items: List[Dict[str, Any]], token: Optional[str] = None) -> Dict[str, Any]:
        sha = hashlib.sha256(json.dumps(tree_items, sort_keys=True).encode("utf-8")).hexdigest()
        self._record("POST", f"/repos/{full_name}/git/trees", sha=sha, items=len(tree_items))
        return {"sha": sha}

    def create_commit(self, full_name: str, message: str, tree_sha: str, parent_shas: List[str], token: Optional[str] = None) -> Dict[str, Any]:
        sha = hashlib.sha256(f"{message}{tree_sha}".encode("utf-8")).hexdigest()
        self._record("POST", f"/repos/{full_name}/git/commits", sha=sha)
        return {"sha": sha}

    def update_patchline_ref(self, full_name: str, branch: str, sha: str, token: Optional[str] = None) -> Dict[str, Any]:
        if not branch.startswith("patchline/"):
            raise ValueError("ref updates are restricted to patchline/* branches")
        self._record("PATCH", f"/repos/{full_name}/git/refs/heads/{branch}", sha=sha)
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}

    def create_pull_request(self, full_name: str, head: str, base: str, title: str, body: str, token: Optional[str] = None) -> Dict[str, Any]:
        self._pr_counter += 1
        number = self._pr_counter
        self._record("POST", f"/repos/{full_name}/pulls", number=number, head=head, base=base, title=title)
        pr = {
            "number": number,
            "html_url": f"https://github.com/{full_name}/pull/{number}",
            "state": "open",
            "head": {"ref": head},
            "base": {"ref": base},
            "title": title,
            "body": body,
        }
        self._opened_prs.append(pr)
        return pr

    def get_pull_request(self, full_name: str, number: int, token: Optional[str] = None) -> Dict[str, Any]:
        self._record("GET", f"/repos/{full_name}/pulls/{number}")
        for pr in self._opened_prs:
            if pr["number"] == number:
                return dict(pr)
        for pr in self._load("pull_requests").get("pull_requests", []):
            if int(pr.get("number", -1)) == number and pr.get("repo_full_name") == full_name:
                return {
                    "number": number,
                    "html_url": pr.get("url") or f"https://github.com/{full_name}/pull/{number}",
                    "state": pr.get("state", "open"),
                    "merged_at": pr.get("merged_at"),
                    "head": {"ref": pr.get("head_branch", "patchline/unknown")},
                }
        return {"number": number, "state": "open", "html_url": f"https://github.com/{full_name}/pull/{number}"}

    def list_pull_requests(self, full_name: str, state: str = "all", token: Optional[str] = None) -> List[Dict[str, Any]]:
        self._record("GET", f"/repos/{full_name}/pulls", state=state)
        out = []
        for pr in self._load("pull_requests").get("pull_requests", []):
            if pr.get("repo_full_name") == full_name:
                out.append(
                    {
                        "number": pr.get("number"),
                        "html_url": pr.get("url"),
                        "state": pr.get("state", "open"),
                        "merged_at": pr.get("merged_at"),
                        "head": {"ref": pr.get("head_branch", "patchline/unknown")},
                    }
                )
        out.extend(dict(p) for p in self._opened_prs)
        return out

    def compare(self, full_name: str, base: str, head: str, token: Optional[str] = None) -> Dict[str, Any]:
        self._record("GET", f"/repos/{full_name}/compare/{base}...{head}")
        # Empty diff by default: branch reuse logic treats this as no match.
        return {"files": [], "ahead_by": 0}

    def close(self) -> None:  # interface parity
        return None
