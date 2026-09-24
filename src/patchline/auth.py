"""Vendor authentication: sessions, allowlist middleware, step-up (FR-001..003,
FR-031/032/033/050/052/060/064).

- One vendor per deployment; GitHub OAuth with single-use ``state``.
- The allowlist is re-read from the environment on EVERY authenticated
  request (FR-052); unset/empty rejects everything; a changed allowlist
  revokes stale sessions immediately.
- Sessions: 7-day idle timeout (absolute); demo sessions 24-hour absolute.
- Step-up (dispatch only): 15 minutes since last_activity_at
  (SESSION_STEP_UP_AGE_SECONDS test override); no-op in demo mode.
- last_activity_at updates are amortized: written at most once per 60s per
  session (the 60-second batch contract of FR-002).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from . import config
from .core.security import generate_csrf_token, new_token
from .db import session_scope

log = logging.getLogger("patchline.auth")

COOKIE_NAME = "patchline_session"
IDLE_TIMEOUT_DAYS = 7
DEMO_ABSOLUTE_HOURS = 24
ACTIVITY_FLUSH_SECONDS = 60

PUBLIC_PATHS = (
    "/login",
    "/login/start",
    "/logout",
    "/auth/github/callback",
    "/healthz",
    "/webhooks/github",
)
PUBLIC_PREFIXES = ("/static",)


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith(PUBLIC_PREFIXES):
        return True
    if path == "/demo" and config.demo_mode():
        return True
    return False


# ---------------------------------------------------------------------------
# Session lifecycle


def create_preauth_session(sess) -> object:
    """OAuth pre-auth session: carries the single-use state (FR-032)."""
    from .models import Session

    row = Session(
        vendor_id=None,
        cookie_value=new_token(),
        oauth_state=new_token(24),
        last_activity_at=datetime.now(timezone.utc),
    )
    sess.add(row)
    sess.flush()
    return row


def create_vendor_session(sess, vendor_id: int, demo: bool = False) -> object:
    from .models import Session

    now = datetime.now(timezone.utc)
    row = Session(
        vendor_id=vendor_id,
        cookie_value=new_token(),
        csrf_token=None,  # minted on first render need; see ensure_csrf
        last_activity_at=now,
        is_demo=demo,
        expires_at=(now + timedelta(hours=DEMO_ABSOLUTE_HOURS)) if demo else None,
    )
    sess.add(row)
    sess.flush()
    ensure_csrf(sess, row)
    return row


def ensure_csrf(sess, session) -> str:
    if not session.csrf_token:
        session.csrf_token = generate_csrf_token(config.session_secret() or "patchline-dev", session.cookie_value)
        sess.flush()
    return session.csrf_token


def load_session_with_vendor(sess, cookie_value: str) -> Tuple[Optional[object], Optional[object]]:
    from .models import Session, Vendor

    if not cookie_value:
        return None, None
    session = sess.query(Session).filter_by(cookie_value=cookie_value, deleted_at=None).one_or_none()
    if session is None:
        return None, None
    vendor = sess.get(Vendor, session.vendor_id) if session.vendor_id else None
    return session, vendor


def session_invalid_reason(session, now: Optional[datetime] = None) -> Optional[str]:
    """None when valid; otherwise the rejection reason."""
    now = now or datetime.now(timezone.utc)
    if session is None:
        return "no session"
    if session.revoked_at is not None:
        return "revoked"
    if session.is_demo:
        created = _aware(session.created_at)
        if now - created > timedelta(hours=DEMO_ABSOLUTE_HOURS):
            return "demo session expired (24h absolute)"
        return None
    if session.expires_at is not None and now >= _aware(session.expires_at):
        return "expired"
    last = _aware(session.last_activity_at)
    if now - last > timedelta(days=IDLE_TIMEOUT_DAYS):
        return "idle timeout (7 days)"
    return None


def needs_step_up(session, now: Optional[datetime] = None, max_age_seconds: Optional[int] = None) -> bool:
    """FR-050: dispatch freshness. Demo mode: step-up is a no-op (FR-060h)."""
    if session is None:
        return True
    if session.is_demo or config.demo_mode():
        return False
    now = now or datetime.now(timezone.utc)
    max_age = max_age_seconds if max_age_seconds is not None else config.step_up_age_seconds()
    return (now - _aware(session.last_activity_at)).total_seconds() > max_age


def revoke_session(sess, session) -> None:
    session.revoked_at = datetime.now(timezone.utc)
    sess.flush()


def revoke_all_sessions(sess) -> int:
    """FR-064: revoke every non-expired, non-revoked session."""
    from .models import Session

    now = datetime.now(timezone.utc)
    rows = sess.query(Session).filter(Session.revoked_at.is_(None)).all()
    count = 0
    for row in rows:
        row.revoked_at = now
        count += 1
    sess.flush()
    return count


def _aware(dt: datetime) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def touch_activity(sess, session, now: Optional[datetime] = None) -> None:
    """FR-002 amortized batching: at most one write per 60s per session."""
    now = now or datetime.now(timezone.utc)
    last = _aware(session.last_activity_at)
    if (now - last).total_seconds() >= ACTIVITY_FLUSH_SECONDS:
        session.last_activity_at = now
        sess.flush()


# ---------------------------------------------------------------------------
# Middleware (FR-003)


class SessionMiddleware:
    """Pure-ASGI middleware: resolves the session, enforces access rules,
    re-reads the allowlist on every authenticated request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.requests import Request
        from starlette.responses import RedirectResponse

        request = Request(scope, receive)
        path = scope.get("path", "/")
        request.state.session = None
        request.state.vendor = None
        request.state.auth_error = None

        if is_public_path(path):
            # Still resolve the session when present so pages can render state.
            cookie = request.cookies.get(COOKIE_NAME)
            if cookie:
                with session_scope(read_only=True) as sess:
                    session, vendor = load_session_with_vendor(sess, cookie)
                    if session is not None and session_invalid_reason(session) is None:
                        request.state.session = session
                        request.state.vendor = vendor
            await self.app(scope, receive, send)
            return

        cookie = request.cookies.get(COOKIE_NAME)
        with session_scope() as sess:
            session, vendor = load_session_with_vendor(sess, cookie or "")
            invalid = session_invalid_reason(session)
            if invalid is not None or vendor is None:
                await _reject(request, scope, receive, send, reason="session")
                return
            if not config.demo_mode():
                allow = config.allowlisted_login()
                if allow is None:
                    await _reject(request, scope, receive, send, reason="misconfigured")
                    return
                if vendor.github_login != allow:
                    revoke_session(sess, session)
                    await _reject(request, scope, receive, send, reason="authorization changed")
                    return
            touch_activity(sess, session)
            request.state.session = session
            request.state.vendor = vendor
            # Detach-friendly copies for templates rendered after the tx closes.
            request.state.csrf_token = session.csrf_token
        await self.app(scope, receive, send)


async def _reject(request, scope, receive, send, reason: str) -> None:
    from starlette.responses import RedirectResponse

    location = "/login"
    if reason == "misconfigured":
        location = "/login?error=misconfigured+allowlist"
    elif reason == "authorization changed":
        location = "/login?error=authorization+changed"
    response = RedirectResponse(url=location, status_code=302)
    if reason != "session":
        response.delete_cookie(COOKIE_NAME)
    await response(scope, receive, send)
