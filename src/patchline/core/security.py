"""Security primitives: tokens, HMAC verification, CSRF, OAuth state,
step-up redirect allowlist (FR-031/032/038/050/069).

All functions are stdlib-only and side-effect free where possible so they are
unit-testable without web or database dependencies.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Optional

# ---------------------------------------------------------------------------
# Random tokens


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# ---------------------------------------------------------------------------
# GitHub webhook HMAC (FR-005): X-Hub-Signature-256: sha256=<hex>


def github_webhook_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_github_signature(secret: str, body: bytes, signature_header: Optional[str]) -> bool:
    """Constant-time verification; unsigned/mismatched payloads rejected (401)."""
    if not secret or not signature_header:
        return False
    expected = github_webhook_signature(secret, body)
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Outgoing notification HMAC (FR-038/069): X-Patchline-Signature: sha256=<hex>


def notification_signature(secret: str, payload_bytes: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# CSRF (FR-031): generated per session, stored on the session row, verified
# constant-time. SESSION_SECRET seeds the token so values are not guessable
# from the cookie alone.


def generate_csrf_token(session_secret: str, cookie_value: str) -> str:
    nonce = secrets.token_urlsafe(16)
    mac = hmac.new(
        session_secret.encode("utf-8"),
        (cookie_value + ":" + nonce).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{nonce}.{mac}"


def verify_csrf_token(stored: Optional[str], presented: Optional[str]) -> bool:
    if not stored or not presented:
        return False
    return hmac.compare_digest(stored, presented)


# ---------------------------------------------------------------------------
# Step-up redirect target allowlist (FR-050). Path-only matching; anything
# not matching a pattern — including protocol-relative and absolute URLs —
# is replaced with "/".


_REDIRECT_PATTERNS = [
    re.compile(r"^/$"),
    re.compile(r"^/entries/[a-zA-Z0-9-]+$"),
    re.compile(r"^/entries/[a-zA-Z0-9-]+/repos/[a-zA-Z0-9_-]+/review$"),
    re.compile(r"^/entries/[a-zA-Z0-9-]+/coverage$"),
    re.compile(r"^/pull-requests$"),
    re.compile(r"^/repositories$"),
    re.compile(r"^/repositories/[a-zA-Z0-9_-]+/coverage$"),
    re.compile(r"^/jobs$"),
    re.compile(r"^/settings$"),
    re.compile(r"^/connections/setup$"),
]


def safe_redirect_target(target: Optional[str]) -> str:
    if not target:
        return "/"
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    if "://" in target:
        return "/"
    path = target.split("?", 1)[0]
    for pattern in _REDIRECT_PATTERNS:
        if pattern.match(path):
            return path
    return "/"


# ---------------------------------------------------------------------------
# Branch-name local validation (FR-040): empty strings and control characters
# (bytes 0x00-0x1F) are rejected locally before any GitHub existence check.


def validate_branch_names(branches: list) -> list:
    """Return a list of validation errors (empty when valid)."""
    errors = []
    if len(branches) > 10:
        errors.append("a maximum of 10 branches per repository is allowed")
    for name in branches:
        if not name or not name.strip():
            errors.append("branch names may not be empty")
            continue
        if any(ord(ch) < 0x20 for ch in name):
            errors.append(f"branch name {name!r} contains control characters")
    return errors
