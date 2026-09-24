"""POST /webhooks/github — HMAC-verified, idempotent (FR-005/FR-035)."""
from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..core.security import verify_github_signature
from ..core.webhook_handlers import process_webhook

log = logging.getLogger("patchline.routes.webhooks")

router = APIRouter()


@router.post("/webhooks/github")
async def github_webhook(request: Request):
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET") or ""
    body = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    if not verify_github_signature(secret, body, signature):
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    delivery_id = request.headers.get("x-github-delivery") or ""
    event_type = request.headers.get("x-github-event") or ""
    if not delivery_id:
        return JSONResponse({"error": "missing X-GitHub-Delivery header"}, status_code=400)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    scm = request.app.state.get_scm() if hasattr(request.app.state, "get_scm") else None
    try:
        result = process_webhook(delivery_id, event_type, payload, scm)
    except Exception as exc:  # noqa: BLE001 - crash → rollback → GitHub retries (FR-005)
        log.exception("webhook %s processing failed", delivery_id)
        return JSONResponse({"error": f"processing failed: {exc}"}, status_code=500)
    return JSONResponse({"status": result}, status_code=200)
