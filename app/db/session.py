"""SQLAlchemy engine + session factory.

Defaults: SQLite at `./data/trading.db` (created on demand). For tests the
`TESTING=1` env var swaps to in-memory SQLite via `Settings.effective_database_url`.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def _ensure_sqlite_dir(url: str) -> None:
    """Create the parent directory of a sqlite file:// URL if needed."""
    if not url.startswith("sqlite:///"):
        return
    path = url[len("sqlite:///") :]
    # `:memory:` has no parent
    if not path or path == ":memory:":
        return
    parent = Path(path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def _build_engine() -> Engine:
    settings = get_settings()
    url = settings.effective_database_url()
    _ensure_sqlite_dir(url)

    connect_args: dict[str, Any] = {}
    engine_kwargs: dict[str, Any] = {
        "connect_args": connect_args,
        "future": True,
    }

    if url.startswith("sqlite"):
        # SQLite needs the same connection for the same in-memory DB, OR
        # a file. We do NOT share a single StaticPool here for file mode —
        # we want each request a fresh connection so commits persist to disk.
        if ":memory:" in url or url.endswith(":memory:"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}
            engine_kwargs["poolclass"] = __import__(
                "sqlalchemy.pool", fromlist=["StaticPool"]
            ).StaticPool
        else:
            connect_args["check_same_thread"] = False
            # Hand back a connection only after it answers. The live DB has
            # corrupted three times (2026-08-07, 2026-08-20..24, 2026-08-25);
            # each time every pooled connection stayed poisoned and served
            # errors until someone restarted the service by hand. pre_ping
            # discards a dead connection instead.
            #
            # ponytail: pool size left at the SQLAlchemy default. Shrinking
            # it to serialise writers is tempting on a single-writer
            # database, but busy_timeout below fixes the contention without
            # risking a pool-exhaustion deadlock. Revisit only if
            # SQLITE_BUSY shows up in the logs despite the timeout.
            engine_kwargs["pool_pre_ping"] = True

    eng = create_engine(url, **engine_kwargs)

    # Foreign keys are off by default in SQLite — turn them on.
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            # Wait up to 10s for the write lock rather than failing the
            # insert. This is the single most important setting here: a
            # news burst puts the monitor, the analyzer, the outcome
            # logger and the dataset builder on the write lock at once.
            cur.execute("PRAGMA busy_timeout=10000")
            # WAL defaults to synchronous=NORMAL, which does NOT fsync the
            # WAL on commit — a host-level stall can then lose or tear the
            # last frames. FULL costs one fsync per commit; this bot
            # commits tens of times a minute, not thousands.
            cur.execute("PRAGMA synchronous=FULL")
            # Checkpoint every ~4 MB of WAL (1000 pages x 4 KB). Without
            # it the WAL only truncates when the last connection closes,
            # and a long-lived pool means that is "never".
            cur.execute("PRAGMA wal_autocheckpoint=1000")
            cur.close()

    return eng


engine: Engine = _build_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a Session, ensures close."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def rebuild_engine_for_testing() -> Engine:
    """Tear down cached engine + SessionLocal and rebuild from current settings.

    Used by tests after they mutate env vars / override `DATABASE_URL`.
    """
    global engine, SessionLocal
    engine.dispose()
    engine = _build_engine()
    SessionLocal = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )
    return engine
