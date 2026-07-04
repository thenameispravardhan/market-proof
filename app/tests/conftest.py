"""Pytest configuration.

- Forces TESTING=1 so config picks in-memory SQLite.
- Rebuilds the SQLAlchemy engine + SessionLocal so they point at the
  in-memory DB.
- Creates all tables (session-scoped — `Base.metadata.create_all` is
  cheap and idempotent).
- Provides a `client` fixture (TestClient) and a `session` fixture.
- The `isolated_db` fixture (opt-in) wipes all tables after the test
  so tests that use `BaseMonitor` (which commits in its own session)
  do not leak rows into other tests' assertions.
"""
from __future__ import annotations

import os

# Set BEFORE any app import so get_settings() sees TESTING=1.
os.environ["TESTING"] = "1"
os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("FYERS_APP_ID", "")
os.environ.setdefault("FYERS_SECRET_KEY", "")
# Keep the real .env's postback secret out of tests so the DB-override
# path (which the postback tests exercise) stays authoritative here.
os.environ.setdefault("FYERS_POSTBACK_SECRET", "")
# Don't enforce IST market hours in tests — auto-entry gating would
# otherwise depend on the wall-clock time the suite happens to run at.
os.environ.setdefault("ENFORCE_MARKET_HOURS", "0")
# Unleveraged sizing baseline: the sizing/cap tests were tuned with
# notional capacity == equity. The 5x intraday leverage is exercised
# explicitly in test_risk_engine.py::test_intraday_leverage_*.
os.environ.setdefault("INTRADAY_LEVERAGE", "1")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import config as app_config  # noqa: E402
from app.db import init as db_init  # noqa: E402
from app.db import session as db_session_mod  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _configure_logging() -> None:
    configure_logging("WARNING")  # quiet logs during tests


@pytest.fixture(scope="session", autouse=True)
def _setup_in_memory_db() -> None:
    app_config.reset_settings_cache()
    db_session_mod.rebuild_engine_for_testing()
    db_init.init_db()


@pytest.fixture()
def isolated_db() -> None:
    """Opt-in fixture: deletes every row from every table after the
    test runs. Use this when a test commits from a thread other than
    the one the `db_session` fixture holds (e.g. `BaseMonitor` ticks
    on an executor thread)."""
    yield
    # Tear down — drop all data. Order matters for FKs; drop_all
    # + create_all is the simplest correct thing.
    with db_session_mod.engine.begin() as conn:
        # SQLite needs a deferred FK drop — we use TRUNCATE-via-DELETE.
        for table in reversed(Base.metadata.sorted_tables):
            try:
                conn.execute(table.delete())
            except Exception:  # noqa: BLE001
                pass
        conn.commit()


@pytest.fixture()
def db_session() -> Session:
    """Yield a SQLAlchemy session bound to the in-memory test DB."""
    s = db_session_mod.SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client() -> TestClient:
    # Import after env is set so app sees TESTING=1.
    from app.main import app

    with TestClient(app) as c:
        yield c
