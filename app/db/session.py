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

    eng = create_engine(url, **engine_kwargs)

    # Foreign keys are off by default in SQLite — turn them on.
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
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
