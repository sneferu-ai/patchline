"""Database engine/session plumbing (FR-047, CP-2).

- WAL journal mode + ``PRAGMA busy_timeout=5000`` on every connection.
- A read-only pool (``PRAGMA query_only=ON``) serves GET traffic so readers
  never contend with the worker's write lock; write paths use a read-write
  connection.
- ``database is locked`` (despite busy_timeout) → retry once after 1 second,
  then surface :class:`DatabaseBusy` which the web layer renders as 503.

SQLAlchemy is imported LAZILY inside each function so that pure-logic core
modules (diffhash, coverage map, dispatch branch logic, …) remain importable
in minimal environments; the dependency is required only when a database is
actually touched.
"""
from __future__ import annotations

import contextlib
import logging
import time
from typing import Iterator, Optional

from . import config

log = logging.getLogger("patchline.db")


class DatabaseBusy(Exception):
    """Raised when SQLite stays locked after busy_timeout plus one retry."""


def _sa():
    try:
        import sqlalchemy  # noqa: F401
        from sqlalchemy import create_engine, event, text  # noqa: F401
        from sqlalchemy.orm import Session, sessionmaker  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "SQLAlchemy is required for database access (poetry install)"
        ) from exc
    import sqlalchemy
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import Session, sessionmaker

    return sqlalchemy, create_engine, event, text, Session, sessionmaker


def _apply_pragmas(dbapi_conn, read_only: bool) -> None:  # pragma: no cover
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    if read_only:
        cur.execute("PRAGMA query_only=ON")
    cur.close()


def make_engine(url: Optional[str] = None, read_only: bool = False):
    sqlalchemy, create_engine, event, _text, _Session, _sessionmaker = _sa()
    url = url or config.database_url()
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True, future=True)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record):  # noqa: ANN001
            _apply_pragmas(dbapi_conn, read_only)

    return engine


_engines = {}


def get_engine(read_only: bool = False):
    """Process-wide engine registry keyed by (url, read_only)."""
    url = config.database_url()
    key = (url, read_only)
    if key not in _engines:
        _engines[key] = make_engine(url, read_only=read_only)
    return _engines[key]


def reset_engines() -> None:
    """Test hook: drop cached engines (e.g. after DATABASE_URL changes)."""
    for eng in _engines.values():
        eng.dispose()
    _engines.clear()


def session_factory(engine: Optional[object] = None):
    _sa_mod, _ce, _ev, _t, Session, sessionmaker = _sa()
    return sessionmaker(bind=engine or get_engine(), class_=Session, expire_on_commit=False, future=True)


@contextlib.contextmanager
def session_scope(engine: Optional[object] = None, read_only: bool = False) -> Iterator:
    """Short-lived session. Commits on success; rolls back on any error.

    FR-047: on a ``database is locked`` OperationalError, retry the COMMIT once
    after 1 second; a second failure raises :class:`DatabaseBusy`.
    """
    _sa_mod, _ce, _ev, _t, _S, _sm = _sa()
    from sqlalchemy.exc import OperationalError

    factory = session_factory(engine or get_engine(read_only=read_only))
    sess = factory()
    try:
        yield sess
        try:
            sess.commit()
        except OperationalError as exc:
            if "database is locked" in str(exc).lower():
                log.warning("database locked on commit; retrying once after 1s")
                time.sleep(1.0)
                try:
                    sess.commit()
                except OperationalError as exc2:
                    sess.rollback()
                    raise DatabaseBusy(str(exc2)) from exc2
            else:
                sess.rollback()
                raise
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def check_database(engine: Optional[object] = None) -> bool:
    """/healthz: dynamic SQL check — executes SELECT 1 (AC-002)."""
    try:
        _sa_mod, _ce, _ev, text, _S, _sm = _sa()
        with (engine or get_engine(read_only=True)).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - health check must never raise
        log.warning("healthz database check failed: %s", exc)
        return False


def integrity_check(engine: Optional[object] = None) -> bool:
    """FR-058: PRAGMA integrity_check after a restore; False refuses startup."""
    try:
        _sa_mod, _ce, _ev, text, _S, _sm = _sa()
        eng = engine or get_engine()
        if not str(eng.url).startswith("sqlite"):
            return True
        with eng.connect() as conn:
            row = conn.execute(text("PRAGMA integrity_check")).scalar()
        return row == "ok"
    except Exception as exc:  # noqa: BLE001
        log.error("integrity_check failed: %s", exc)
        return False
