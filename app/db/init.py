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
    if first_time:
        _logged_engines.add(engine)
        log.info("init_db.done", tables=len(Base.metadata.tables))
