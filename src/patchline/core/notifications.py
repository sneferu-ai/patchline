"""Outgoing webhook notifications (FR-038/FR-069).

HMAC-SHA256-signed POSTs with a stable event_type enum, UUID delivery_id for
receiver-side idempotency, and a Config-persisted instance_id. Never sent
unsigned: if exactly one of URL/secret is configured the system starts with
notifications disabled (and verify-config reports the pairing error).

Deliveries are retried 3 times with exponential backoff, then logged as failed.
"""
from __future__ import annotations

import json
import logging
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .. import config
from .security import notification_signature

log = logging.getLogger("patchline.notifications")

EVENT_TYPES = (
    "scan_complete",
    "pr_dispatched",
    "pr_merged",
    "pr_rejected",
    "engine_failure",
    "regression_detected",
)

MAX_DELIVERY_ATTEMPTS = 3


def pairing_status() -> str:
    """'ok' (both set or both unset) or 'unpaired' (exactly one set)."""
    url = config.notification_webhook_url()
    secret = config.notification_signing_secret()
    if bool(url) == bool(secret):
        return "ok"
    return "unpaired"


def notifications_enabled() -> bool:
    return pairing_status() == "ok" and config.notification_webhook_url() is not None


def engine_failure_next_attempt_at(failures: int, now: Optional[datetime] = None) -> datetime:
    """engine_failure payload field: exp backoff 60s→3600s, ×2, ±20% jitter."""
    now = now or datetime.now(timezone.utc)
    base = min(3600.0, 60.0 * (2 ** max(failures - 1, 0)))
    spread = base * 0.2
    delay = base + random.uniform(-spread, spread)
    return now + timedelta(seconds=max(1.0, delay))


def build_payload(
    event_type: str,
    *,
    instance_id: str,
    entry_id: Optional[int] = None,
    repository_name: Optional[str] = None,
    status: Optional[str] = None,
    delivery_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
    next_attempt_at: Optional[datetime] = None,
    regression_type: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """FR-038 normative payload shape."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown notification event_type: {event_type!r}")
    ts = (timestamp or datetime.now(timezone.utc)).isoformat()
    payload: Dict[str, Any] = {
        "event_type": event_type,
        "delivery_id": delivery_id or str(uuid.uuid4()),
        "timestamp": ts,
        "entry_id": entry_id,
        "repository_name": repository_name,
        "status": status,
        "instance_id": instance_id,
    }
    if event_type == "engine_failure":
        payload["next_attempt_at"] = (next_attempt_at or engine_failure_next_attempt_at(1)).isoformat()
    if event_type == "regression_detected":
        if regression_type not in ("reintroduction", "new_introduction"):
            raise ValueError("regression_detected requires regression_type")
        payload["regression_type"] = regression_type
    if extra:
        payload.update(extra)
    return payload


def deliver(payload: Dict[str, Any], url: Optional[str] = None, secret: Optional[str] = None) -> bool:
    """Sign and POST the payload; retry 3x with exponential backoff.

    Returns True on a 2xx delivery. Never sends unsigned (FR-069): without
    BOTH url and secret this is a loud no-op.
    """
    url = url or config.notification_webhook_url()
    secret = secret or config.notification_signing_secret()
    if not url or not secret:
        log.warning("notification %s suppressed: webhook URL/secret not both configured", payload.get("event_type"))
        return False
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Patchline-Signature": notification_signature(secret, body),
    }
    try:
        import httpx
    except ImportError:  # pragma: no cover - sandbox without httpx
        log.warning("notification %s not delivered: httpx unavailable", payload.get("event_type"))
        return False
    for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, content=body, headers=headers)
            if 200 <= resp.status_code < 300:
                return True
            log.warning("notification %s attempt %d -> %s", payload.get("event_type"), attempt, resp.status_code)
        except Exception as exc:  # noqa: BLE001
            log.warning("notification %s attempt %d failed: %s", payload.get("event_type"), attempt, exc)
        if attempt < MAX_DELIVERY_ATTEMPTS:
            time.sleep(min(8.0, 1.0 * (2 ** attempt)))
    log.error("notification %s delivery %s failed after %d attempts", payload.get("event_type"), payload.get("delivery_id"), MAX_DELIVERY_ATTEMPTS)
    return False


def record_and_deliver(
    sess,
    event_type: str,
    *,
    instance_id: str,
    deliver_now: bool = False,
    **payload_kwargs: Any,
):
    """Persist a Notification row. Delivery is decoupled: callers inside a
    database transaction queue with ``deliver_now=False`` and call
    :func:`flush_pending` AFTER the transaction commits, so no network I/O is
    ever performed while a SQLite write lock is held (CP-2).
    """
    from ..models import Notification

    payload = build_payload(event_type, instance_id=instance_id, **payload_kwargs)
    row = Notification(
        event_type=event_type,
        payload_json=json.dumps(payload, sort_keys=True),
        webhook_url=config.notification_webhook_url(),
        status="pending",
        delivery_id=payload["delivery_id"],
    )
    sess.add(row)
    sess.flush()
    if not notifications_enabled():
        row.status = "disabled"
        sess.flush()
        return row, payload, False
    if deliver_now:
        ok = deliver(payload)
        row.status = "sent" if ok else "failed"
        row.attempts += 1
        sess.flush()
        return row, payload, ok
    return row, payload, False


def flush_pending(limit: int = 50) -> int:
    """Deliver queued notifications OUTSIDE any transaction.

    Loads pending rows in a fresh short transaction, attempts delivery
    (network), then records outcomes in a second short transaction. Receiver
    side stays idempotent via delivery_id, so re-flushing is safe.
    """
    from ..db import session_scope
    from ..models import Notification

    if not notifications_enabled():
        return 0
    with session_scope() as sess:
        rows = (
            sess.query(Notification)
            .filter(Notification.status == "pending")
            .order_by(Notification.id.asc())
            .limit(limit)
            .all()
        )
        pending = [(row.id, row.payload_json) for row in rows]
    delivered = 0
    for notification_id, payload_json in pending:
        try:
            payload = json.loads(payload_json)
        except ValueError:
            payload = None
        ok = deliver(payload) if payload else False
        with session_scope() as sess:
            row = sess.get(Notification, notification_id)
            if row is not None:
                row.status = "sent" if ok else "failed"
                row.attempts += 1
        if ok:
            delivered += 1
    return delivered
