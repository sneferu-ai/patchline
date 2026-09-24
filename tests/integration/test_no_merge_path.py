"""AC-029 / FR-022 / FR-071: merge-path structural exclusion.

(a) static analysis: no method named *merge*/*squash* exists on the GitHub API
    client class, and the module source contains no merge/squash endpoints;
(b) attempting to call a merge endpoint raises AttributeError.
"""
from __future__ import annotations

import inspect
import os

import pytest

from patchline.adapters.scm import github
from patchline.adapters.scm.github import GitHubClient


class TestStaticExclusion:
    def test_no_merge_like_methods_on_client(self):
        for name, _member in inspect.getmembers(GitHubClient):
            lowered = name.lower()
            assert "merge" not in lowered, f"forbidden method on GitHubClient: {name}"
            assert "squash" not in lowered, f"forbidden method on GitHubClient: {name}"

    def test_module_source_has_no_merge_endpoints(self, repo_root):
        path = os.path.join(repo_root, "src", "patchline", "adapters", "scm", "github.py")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        assert "/merge" not in source
        assert "squash" not in source.lower()
        # No outbound call to the PR-merge endpoint shape anywhere in the client.
        assert "pulls/" not in source or "/merge" not in source

    def test_no_merge_methods_defined(self, repo_root):
        """grep-equivalent of AC-029(a) over the client module."""
        path = os.path.join(repo_root, "src", "patchline", "adapters", "scm", "github.py")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read().lower()
        for needle in ("def merge", "def squash", "merge_pull", "put /repos"):
            assert needle not in source

    def test_ref_updates_restricted_to_patchline_refs(self):
        """FR-071: update_patchline_ref refuses non-patchline branches."""
        client = GitHubClient.__new__(GitHubClient)  # no __init__ (no httpx needed)
        with pytest.raises(ValueError):
            client.update_patchline_ref("acme/widgets", "main", "abc123")


class TestRuntimeExclusion:
    def test_merge_call_raises_attribute_error(self):
        client = GitHubClient.__new__(GitHubClient)
        with pytest.raises(AttributeError):
            client.merge_pull_request("acme/widgets", 1)
        with pytest.raises(AttributeError):
            client.squash_pull_request("acme/widgets", 1)
