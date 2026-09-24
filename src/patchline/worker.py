"""Background job worker + periodic rescan scheduler (MANDATORY process, §6/§8).

- Claims scan jobs atomically (FR-048: priority DESC, created_at ASC,
  next_attempt_at skip) and executes them.
- Processes derivation (draft entries) and remediation ('generating') queues.
- Writes a heartbeat every 30s (FR-048); requeues stale claims on startup.
- The rescan scheduler is a threading.Timer loop in THIS process
  (FR-063 — no external cron); RESCAN_INTERVAL_SECONDS overrides for tests.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

from . import config
from .adapters.engine import create_adapter
from .db import session_scope

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","event":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("patchline.worker")

HEARTBEAT_SECONDS = 30
POLL_IDLE_SECONDS = 1.0

_stop = threading.Event()


def write_heartbeat(path: str | None = None) -> None:
    target = path or config.worker_heartbeat_path()
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(datetime.now(timezone.utc).isoformat())
    except OSError as exc:
        log.warning("heartbeat write failed (%s): %s", target, exc)


def _heartbeat_loop() -> None:
    while not _stop.is_set():
        write_heartbeat()
        _stop.wait(HEARTBEAT_SECONDS)


def _rescan_scheduler_loop(engine_adapter, get_scm) -> None:
    """FR-063: enqueue periodic rescans at the configured interval."""
    from .core import jobs
    from .models import ChangelogEntry, Repository

    interval = config.rescan_interval_seconds()
    # Fire once shortly after boot so short test cadences (1-2s) are exercisable,
    # then every interval thereafter.
    first_delay = min(interval, 2.0) if interval <= 5 else interval
    if _stop.wait(first_delay):
        return
    while not _stop.is_set():
        try:
            with session_scope() as sess:
                entries = sess.query(ChangelogEntry).filter(ChangelogEntry.deleted_at.is_(None)).all()
                repos = {r.id: r for r in sess.query(Repository).filter(Repository.deleted_at.is_(None)).all()}
                created = jobs.enqueue_periodic_rescans(sess, entries, repos)
                if created:
                    log.info("periodic rescan enqueued %d job(s)", len(created))
        except Exception as exc:  # noqa: BLE001 - scheduler must never die
            log.exception("periodic rescan enqueue failed: %s", exc)
        if _stop.wait(interval):
            return


def process_one_job(engine_adapter, scm) -> bool:
    """Claim and execute a single queued scan job. True when work happened."""
    from .core import jobs
    from .core.scanning import execute_scan_job

    with session_scope() as sess:
        job = jobs.claim_next_job(sess)
        job_id = job.id if job else None
    if job_id is None:
        return False
    result = execute_scan_job(job_id, engine_adapter, scm)
    log.info("scan job %s finished with %s", job_id, result)
    return True


def process_pending_remediations(engine_adapter, scm) -> int:
    from .core.remediation_flow import generate_remediation
    from .models import Remediation

    with session_scope() as sess:
        ids = [
            row.id
            for row in sess.query(Remediation)
            .filter(Remediation.status == "generating", Remediation.deleted_at.is_(None))
            .order_by(Remediation.id.asc())
            .limit(5)
            .all()
        ]
    for remediation_id in ids:
        generate_remediation(remediation_id, engine_adapter, scm)
    return len(ids)


def run_worker(once: bool = False) -> None:
    from .core import derivation, jobs

    engine_adapter = create_adapter()
    if config.demo_mode():
        from .adapters.scm.mock_github import FixtureGitHubClient

        scm = FixtureGitHubClient()
    else:
        from .adapters.scm.github import GitHubClient

        scm = GitHubClient()

    with session_scope() as sess:
        requeued = jobs.requeue_stale_jobs(sess)
        if requeued:
            log.info("requeued %d stale job(s) on startup", requeued)

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, name="patchline-heartbeat", daemon=True)
    heartbeat_thread.start()
    scheduler_thread = threading.Thread(
        target=_rescan_scheduler_loop, args=(engine_adapter, lambda: scm), name="patchline-rescan", daemon=True
    )
    scheduler_thread.start()
    log.info("worker started (engine_mode=%s, rescan every %ss)", config.engine_mode(), config.rescan_interval_seconds())

    while not _stop.is_set():
        did_work = False
        try:
            if derivation.process_pending_derivations(engine_adapter):
                did_work = True
            if process_pending_remediations(engine_adapter, scm):
                did_work = True
            if process_one_job(engine_adapter, scm):
                did_work = True
        except Exception as exc:  # noqa: BLE001 - the worker must never die on a bad job
            log.exception("worker pass failed: %s", exc)
        if once:
            break
        if not did_work:
            _stop.wait(POLL_IDLE_SECONDS)


def main() -> None:
    try:
        run_worker()
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()


if __name__ == "__main__":
    main()
