"""Machine endpoints: /healthz (+ ?deep=1) — public (FR-048, AC-002/AC-008)."""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config
from ..db import check_database

router = APIRouter()


def _worker_status() -> str:
    path = config.worker_heartbeat_path()
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return "unknown"
    return "healthy" if age < 120 else "stale"


def _engine_status(request: Request) -> str:
    """AC-008: engine reachability — mock is always healthy; sneferu mode
    performs an actual HTTP ping of the configured endpoint."""
    if config.engine_mode() == "mock" or config.demo_mode():
        return "healthy"
    engine = getattr(request.app.state, "engine", None)
    ping = getattr(engine, "ping", None)
    if ping is None:
        return "unknown"
    try:
        return "healthy" if ping() else "unreachable"
    except Exception:  # noqa: BLE001
        return "unreachable"


@router.get("/healthz")
def healthz(request: Request, deep: int = 0):
    db_ok = check_database()
    if not db_ok:
        return JSONResponse({"status": "unhealthy", "database": "disconnected"}, status_code=503)
    payload = {"status": "healthy", "database": "connected"}
    if deep:
        engine = _engine_status(request)
        payload["engine"] = engine
        payload["engine_mode"] = "mock" if config.demo_mode() else config.engine_mode()
        payload["worker"] = _worker_status()
        if engine == "unreachable":
            payload["status"] = "unhealthy"
            return JSONResponse(payload, status_code=503)
    return JSONResponse(payload, status_code=200)
