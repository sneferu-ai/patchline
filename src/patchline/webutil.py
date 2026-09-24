"""Web helpers: dependency-free urlencoded form parsing, CSRF enforcement,
template rendering context. (Form bodies are application/x-www-form-urlencoded;
parsed with urllib so no python-multipart dependency is required.)
"""
from __future__ import annotations

from typing import Dict, Optional
from urllib.parse import parse_qs

from .core.security import verify_csrf_token


async def form_data(request) -> Dict[str, str]:
    """Parse a urlencoded form body into a flat dict (last value wins)."""
    body = await request.body()
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def presented_csrf_token(request, form: Optional[Dict[str, str]] = None) -> Optional[str]:
    header = request.headers.get("x-csrf-token")
    if header:
        return header
    if form:
        return form.get("csrf_token")
    return None


def csrf_ok(request, form: Optional[Dict[str, str]] = None) -> bool:
    """FR-031: every state-changing POST must carry the session's token."""
    session = getattr(request.state, "session", None)
    stored = getattr(session, "csrf_token", None) or getattr(request.state, "csrf_token", None)
    return verify_csrf_token(stored, presented_csrf_token(request, form))


def base_context(request, **extra) -> Dict:
    """Common template context: demo banner, csrf, vendor."""
    from . import APP_VERSION

    ctx = {
        "request": request,
        "demo_mode": _demo(),
        "csrf_token": getattr(request.state, "csrf_token", None),
        "vendor": getattr(request.state, "vendor", None),
        "app_version": APP_VERSION,
    }
    ctx.update(extra)
    return ctx


def _demo() -> bool:
    from . import config

    return config.demo_mode()
