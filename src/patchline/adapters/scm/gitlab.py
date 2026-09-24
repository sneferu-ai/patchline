"""Reserved GitLab adapter seam (K1/B12: phase 2, behind the repository port).

GitHub.com cloud is the only supported SCM in this slice. This stub keeps the
adapter slot visible so a GitLab.com implementation can be added without
touching domain logic.
"""
from __future__ import annotations


class GitLabClient:
    """Phase-2 reserved seam. Not implemented in the MVP."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "GitLab support is reserved for phase 2; the MVP supports GitHub.com only"
        )
