"""Database initialisation helper.

`init_db()` is idempotent — `Base.metadata.create_all()` issues a CREATE
TABLE IF NOT EXISTS, so calling it on every startup is cheap and safe.
"""
from __future__ import annotations

from app.db import session as _session
from app.db.session import Base
from app.logging_config import get_logger

# Importing the models module registers them on `Base.metadata`.
from app.db import models as _models  # noqa: F401

log = get_logger(__name__)


def init_db() -> None:
    """Create all tables. Idempotent.

    Uses `_session.engine` dynamically (re-fetched on every call) so that
    tests that rebuild the engine still hit the right one.
    """
    log.info("init_db.start", tables=len(Base.metadata.tables))
    Base.metadata.create_all(bind=_session.engine)
    log.info("init_db.done", tables=len(Base.metadata.tables))
