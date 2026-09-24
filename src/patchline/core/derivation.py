"""Pattern derivation (FR-010): draft entries → ready | derivation_failed.

Derivation jobs are the DB itself: entries persisted as ``draft`` are claimed
by the worker (or driven directly in tests) and derived through the engine
adapter. Feedback flags from prior same-language entries ride along
(FR-039); the mock ignores them by design, the real adapter forwards them.
"""
from __future__ import annotations

import json
import logging
import time
from typing import List, Optional

from ..adapters.engine import EngineError, EntryInput, FeedbackSignal
from ..adapters.languages import is_installed
from ..db import session_scope
from . import config_store

log = logging.getLogger("patchline.derivation")


def feedback_for_language(sess, language: str) -> List[FeedbackSignal]:
    """All feedback signals from prior entries with the same language (FR-039)."""
    from ..models import ChangelogEntry, FeedbackFlag, Occurrence, ScanJob

    signals: List[FeedbackSignal] = []
    rows = (
        sess.query(FeedbackFlag, Occurrence, ChangelogEntry)
        .join(Occurrence, FeedbackFlag.occurrence_id == Occurrence.id)
        .join(ScanJob, Occurrence.scan_job_id == ScanJob.id)
        .join(ChangelogEntry, ScanJob.entry_id == ChangelogEntry.id)
        .filter(ChangelogEntry.language == language)
        .filter(FeedbackFlag.deleted_at.is_(None))
        .all()
    )
    for flag, occurrence, _entry in rows:
        signals.append(
            FeedbackSignal(
                occurrence_snippet=(occurrence.snippet or "")[:500],
                reason=flag.reason or "",
                created_at=flag.created_at.isoformat() if flag.created_at else "",
            )
        )
    return signals


def derive_entry(entry_id: int, engine) -> str:
    """Derive one entry. Returns 'ready' or 'derivation_failed'."""
    from ..models import ChangelogEntry, DerivedPattern

    with session_scope() as sess:
        entry = sess.get(ChangelogEntry, entry_id)
        if entry is None or entry.status != "draft":
            return "skipped"
        entry_input = EntryInput(
            prior_api_shape=entry.prior_api_shape,
            target_contract=entry.target_contract,
            language=entry.language,
        )
        feedback = feedback_for_language(sess, entry.language)
        language = entry.language

    started = time.monotonic()
    try:
        pattern_set = engine.derive_patterns(entry_input, feedback=feedback)
    except EngineError as exc:
        with session_scope() as sess:
            entry = sess.get(ChangelogEntry, entry_id)
            entry.status = "derivation_failed"
            config_store.record_engine_failure(sess)
        log.warning("derivation for entry %s failed: %s", entry_id, exc)
        return "derivation_failed"
    except Exception as exc:  # noqa: BLE001
        with session_scope() as sess:
            entry = sess.get(ChangelogEntry, entry_id)
            entry.status = "derivation_failed"
        log.exception("derivation for entry %s crashed: %s", entry_id, exc)
        return "derivation_failed"

    latency = time.monotonic() - started
    with session_scope() as sess:
        entry = sess.get(ChangelogEntry, entry_id)
        config_store.record_engine_success(sess)
        config_store.record_engine_latency(sess, latency)
        # FR-010: returned patterns must be compatible with the entry language.
        if pattern_set.language and pattern_set.language.strip().lower() != language.strip().lower():
            entry.status = "derivation_failed"
            log.warning(
                "derivation language mismatch: engine returned %r for entry language %r",
                pattern_set.language,
                language,
            )
            return "derivation_failed"
        if not is_installed(pattern_set.language or language):
            entry.status = "derivation_failed"
            return "derivation_failed"
        sess.add(
            DerivedPattern(
                entry_id=entry.id,
                pattern_json=json.dumps(pattern_set.patterns),
                engine_run_id=pattern_set.engine_run_id,
            )
        )
        entry.status = "ready"  # immutable from here (FR-041)
    return "ready"


def process_pending_derivations(engine, limit: int = 10) -> int:
    """Worker pass: derive up to ``limit`` draft entries. Returns count."""
    from ..models import ChangelogEntry

    with session_scope() as sess:
        ids = [
            row.id
            for row in sess.query(ChangelogEntry)
            .filter_by(status="draft", deleted_at=None)
            .order_by(ChangelogEntry.id.asc())
            .limit(limit)
            .all()
        ]
    processed = 0
    for entry_id in ids:
        if derive_entry(entry_id, engine) in ("ready", "derivation_failed"):
            processed += 1
    return processed
