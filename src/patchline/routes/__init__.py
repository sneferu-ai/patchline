"""Route registry."""
from __future__ import annotations

from . import actions, auth, health, surfaces, webhooks

ALL_ROUTERS = [
    health.router,
    webhooks.router,
    auth.router,
    surfaces.router,
    actions.router,
]

__all__ = ["ALL_ROUTERS"]
