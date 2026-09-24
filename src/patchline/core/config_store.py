"""Config-table helpers (CP-6 precedence, FR-027 failure count, FR-038 instance id)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from .. import config as env_config


def get_config_value(sess, key: str) -> Optional[str]:
    from ..models import Config

    row = sess.query(Config).filter_by(key=key).one_or_none()
    return row.value if row else None


def set_config_value(sess, key: str, value: str) -> None:
    from ..models import Config

    row = sess.query(Config).filter_by(key=key).one_or_none()
    if row is None:
        row = Config(key=key, value=value)
        sess.add(row)
    else:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    sess.flush()


def default_language(sess) -> str:
    """FR-026/CP-6: Config table value wins at runtime; the env var seeds the
    table on first startup only; env is the fallback when no row exists."""
    row_value = get_config_value(sess, "default_language")
    if row_value:
        return row_value
    return env_config.default_language_env()


def seed_default_language(sess) -> None:
    """First-startup seeding: env var → Config table, only when no row exists."""
    if get_config_value(sess, "default_language") is None:
        set_config_value(sess, "default_language", env_config.default_language_env())


def instance_id(sess) -> str:
    """FR-038: INSTANCE_ID env var wins; otherwise a stable generated id
    persisted in Config (never hostname — it changes on container restart)."""
    env_value = env_config.instance_id_env()
    if env_value:
        return env_value
    existing = get_config_value(sess, "instance_id")
    if existing:
        return existing
    generated = f"patchline-{uuid.uuid4().hex[:12]}"
    set_config_value(sess, "instance_id", generated)
    return generated


def engine_consecutive_failures(sess) -> int:
    raw = get_config_value(sess, "engine_consecutive_failures")
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0


def record_engine_failure(sess) -> int:
    """FR-027: persisted in Config so it survives worker restarts."""
    count = engine_consecutive_failures(sess) + 1
    set_config_value(sess, "engine_consecutive_failures", str(count))
    return count


def record_engine_success(sess) -> None:
    set_config_value(sess, "engine_consecutive_failures", "0")


_LATENCY_KEY = "engine_latencies"
_LATENCY_CAP = 200


def record_engine_latency(sess, seconds: float) -> None:
    """FR-074: ring buffer of adapter-call latencies for the /metrics p95."""
    import json

    raw = get_config_value(sess, _LATENCY_KEY)
    try:
        values = json.loads(raw) if raw else []
        if not isinstance(values, list):
            values = []
    except ValueError:
        values = []
    values.append(round(float(seconds), 4))
    values = values[-_LATENCY_CAP:]
    set_config_value(sess, _LATENCY_KEY, json.dumps(values))


def record_engine_latency_safe(seconds: float) -> None:
    """Latency recording that never raises into the job path."""
    try:
        from ..db import session_scope

        with session_scope() as sess:
            record_engine_latency(sess, seconds)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("patchline.config_store").warning("engine latency record failed", exc_info=True)


def engine_latency_p95(sess) -> float:
    import json

    raw = get_config_value(sess, _LATENCY_KEY)
    try:
        values = sorted(float(v) for v in (json.loads(raw) if raw else []))
    except (ValueError, TypeError):
        values = []
    if not values:
        return 0.0
    idx = min(len(values) - 1, int(round(0.95 * (len(values) - 1))))
    return values[idx]
