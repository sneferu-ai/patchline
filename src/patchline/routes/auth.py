"""P1 — sign-in / sign-out, OAuth callback, demo entry (FR-001/002/003/032/033/060)."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config
from ..core.security import safe_redirect_target
from ..db import session_scope

log = logging.getLogger("patchline.routes.auth")

router = APIRouter()

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"


def _templates(request: Request):
    return request.app.state.templates


def _demo_login(request: Request):
    """FR-060a: pre-seeded demo session, synthetic vendor, no OAuth. The demo
    board is populated from the GitHub fixtures on first login (AC-028)."""
    from ..models import Vendor

    with session_scope() as sess:
        vendor = sess.query(Vendor).filter_by(github_login="demo-vendor").one_or_none()
        if vendor is None:
            vendor = Vendor(github_user_id=0, github_login="demo-vendor")
            sess.add(vendor)
            sess.flush()
        session = auth.create_vendor_session(sess, vendor.id, demo=True)
        cookie = session.cookie_value
    try:
        from ..core.recheck import recheck_installations

        recheck_installations(request.app.state.get_scm())
    except Exception as exc:  # noqa: BLE001 - demo seeds best-effort
        log.warning("demo fixture seeding failed: %s", exc)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        auth.COOKIE_NAME,
        cookie,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return response


@router.get("/demo")
def demo_entry(request: Request):
    if not config.demo_mode():
        return RedirectResponse(url="/login", status_code=302)
    return _demo_login(request)


@router.get("/login")
def login(request: Request, error: str = "", redirect_to: str = "", step_up: int = 0):
    if config.demo_mode():
        return _demo_login(request)
    return _templates(request).TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "demo_mode": False,
            "error": error,
            "redirect_to": safe_redirect_target(redirect_to),
            "step_up": bool(step_up),
            "app_version": request.app.state.app_version,
        },
    )


@router.get("/login/start")
def login_start(request: Request, redirect_to: str = ""):
    """Begin OAuth: single-use state stored on a pre-auth session (FR-032).

    Concurrent tabs: an existing pre-auth session is REUSED and its state
    overwritten, so the second tab invalidates the first (FR-032 note).
    """
    if config.demo_mode():
        return _demo_login(request)
    target = safe_redirect_target(redirect_to)
    client_id = os.environ.get("GITHUB_CLIENT_ID") or ""
    cookie = request.cookies.get(auth.COOKIE_NAME)
    with session_scope() as sess:
        session = None
        if cookie:
            session, _vendor = auth.load_session_with_vendor(sess, cookie)
            if session is not None and session.vendor_id is not None:
                session = None  # already authenticated; start fresh pre-auth
        if session is None:
            session = auth.create_preauth_session(sess)
        else:
            from ..core.security import new_token

            session.oauth_state = new_token(24)
        state = session.oauth_state
        cookie_value = session.cookie_value

    callback_uri = str(request.url_for("github_callback"))
    if target != "/":
        callback_uri += "?" + urlencode({"redirect_to": target})
    authorize = (
        f"{GITHUB_AUTHORIZE_URL}?client_id={quote(client_id)}"
        f"&redirect_uri={quote(callback_uri, safe='')}"
        f"&state={quote(state)}"
    )
    response = RedirectResponse(url=authorize, status_code=302)
    response.set_cookie(
        auth.COOKIE_NAME,
        cookie_value,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return response


@router.get("/auth/github/callback", name="github_callback")
def github_callback(request: Request, code: str = "", state: str = "", redirect_to: str = "", error: str = ""):
    if error:
        return _templates(request).TemplateResponse(
            request,
            "error.html",
            {"request": request, "status": 400, "message": f"GitHub sign-in was denied ({error}). You can retry below.", "demo_mode": False, "app_version": request.app.state.app_version},
            status_code=400,
        )
    cookie = request.cookies.get(auth.COOKIE_NAME) or ""
    target = safe_redirect_target(redirect_to)
    with session_scope() as sess:
        session, _v = auth.load_session_with_vendor(sess, cookie)
        stored = getattr(session, "oauth_state", None) if session is not None else None
        if not state or not stored or state != stored:
            return _templates(request).TemplateResponse(
                request,
                "error.html",
                {"request": request, "status": 400, "message": "OAuth state mismatch. Sign-in was not completed; start again.", "demo_mode": False, "app_version": request.app.state.app_version},
                status_code=400,
            )
        # FR-032: state is single-use — delete before any further processing.
        session.oauth_state = None
        sess.flush()

    scm = request.app.state.get_scm(real=True)
    try:
        token_data = scm.exchange_oauth_code(
            os.environ.get("GITHUB_CLIENT_ID") or "",
            os.environ.get("GITHUB_CLIENT_SECRET") or "",
            code,
        )
        access_token = token_data.get("access_token")
        gh_user = scm.get_authenticated_user(access_token)
    except Exception as exc:  # noqa: BLE001
        log.warning("oauth exchange failed: %s", exc)
        return _templates(request).TemplateResponse(
            request,
            "error.html",
            {"request": request, "status": 400, "message": "Could not complete GitHub sign-in. Please retry.", "demo_mode": False, "app_version": request.app.state.app_version},
            status_code=400,
        )

    login_name = (gh_user or {}).get("login") or ""
    allow = config.allowlisted_login()
    if allow is None or login_name != allow:
        return _templates(request).TemplateResponse(
            request,
            "error.html",
            {"request": request, "status": 403, "message": "This GitHub account is not authorized for this Patchline deployment.", "demo_mode": False, "app_version": request.app.state.app_version},
            status_code=403,
        )

    from ..models import Vendor

    with session_scope() as sess:
        vendor = sess.query(Vendor).filter_by(github_login=login_name).one_or_none()
        if vendor is None:
            vendor = Vendor(github_user_id=int(gh_user.get("id") or 0), github_login=login_name)
            sess.add(vendor)
            sess.flush()
        session, _ = auth.load_session_with_vendor(sess, cookie)
        session.vendor_id = vendor.id
        session.last_activity_at = datetime.now(timezone.utc)
        session.is_demo = False
        session.csrf_token = None
        auth.ensure_csrf(sess, session)  # fresh CSRF for the re-rendered page (FR-050)
        sess.flush()
    return RedirectResponse(url=target, status_code=302)


@router.get("/logout")
@router.post("/logout")
def logout(request: Request):
    cookie = request.cookies.get(auth.COOKIE_NAME) or ""
    with session_scope() as sess:
        session, _ = auth.load_session_with_vendor(sess, cookie)
        if session is not None:
            auth.revoke_session(sess, session)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(auth.COOKIE_NAME)
    return response
