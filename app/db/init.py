"""Database initialisation helper.

`init_db()` is idempotent — `Base.metadata.create_all()` issues a CREATE
TABLE IF NOT EXISTS, so calling it on every startup is cheap and safe.
"""
from __future__ import annotations

from weakref import WeakSet

from app.db import session as _session
from app.db.session import Base
from app.logging_config import get_logger

# Importing the models module registers them on `Base.metadata`.
from app.db import models as _models  # noqa: F401

log = get_logger(__name__)

# Engines we've already logged a create_all for. `create_all` always
# runs (it's a cheap idempotent CREATE TABLE IF NOT EXISTS), but the
# many import-time callers — each API module guards its tables this
# way — shouldn't spam the startup log with a start/done pair each.
# A WeakSet means a collected engine never keeps anything alive and,
# at worst, a reused engine logs one extra line.
_logged_engines: "WeakSet[object]" = WeakSet()


def init_db() -> None:
    """Create all tables. Idempotent.

    Uses `_session.engine` dynamically (re-fetched on every call) so that
    tests that rebuild the engine still hit the right one. The first
    create_all per engine logs at INFO; subsequent redundant calls run
    silently (the schema is already there).
    """
    engine = _session.engine
    first_time = engine not in _logged_engines
    if first_time:
        log.info("init_db.start", tables=len(Base.metadata.tables))
    Base.metadata.create_all(bind=engine)
    _ensure_columns(engine)
    if first_time:
        _logged_engines.add(engine)
        log.info("init_db.done", tables=len(Base.metadata.tables))


# Columns added to existing tables in later model versions. `create_all`
# only CREATEs missing tables — it never ALTERs an existing one — so we
# add these by hand. Idempotent and SQLite-friendly.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "positions": [("stop_loss", "FLOAT"), ("target", "FLOAT")],
    "trades": [("slippage_pct", "FLOAT"), ("r_multiple", "FLOAT")],
}


def _ensure_columns(engine: object) -> None:
    from sqlalchemy import inspect as _inspect, text

    try:
        insp = _inspect(engine)
        tables = set(insp.get_table_names())
        for table, cols in _ADDED_COLUMNS.items():
            if table not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            missing = [(n, t) for n, t in cols if n not in have]
            if not missing:
                continue
            with engine.begin() as conn:  # type: ignore[attr-defined]
                for name, sqltype in missing:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {name} {sqltype}'))
            log.info("init_db.added_columns", table=table, columns=[n for n, _ in missing])
    except Exception:  # noqa: BLE001 — never block startup on a migration hiccup
        log.exception("init_db.ensure_columns_failed")
