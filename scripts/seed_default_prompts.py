"""Seed DEFAULT + 15 per-event-type prompt templates into the DB.

Idempotent: running twice yields 16 rows total (15 + DEFAULT). New
runs INSERT; re-runs UPDATE the existing row and bump `version` if
any field actually changed.

Usage:
    .venv/Scripts/python.exe scripts/seed_default_prompts.py

This is also import-safe: `seed(session)` is the function used by the
tests. The script's `main()` just wraps it with the standard
engine/session setup.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable when run as a script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app.analyzer.prompts import seed_defaults  # noqa: E402
from app.analyzer.schemas import EVENT_TYPES  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import init as db_init  # noqa: E402
from app.db.models import PromptTemplate  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging, get_logger  # noqa: E402

log = get_logger("scripts.seed_default_prompts")


def seed(session=None) -> int:
    """Seed the DB. Returns the number of rows touched.

    If `session` is None, opens a fresh one and commits. Otherwise
    uses the caller's session (and does NOT commit — caller controls
    the transaction; useful in tests).
    """
    owns_session = session is None
    if owns_session:
        # Make sure the schema exists; idempotent.
        db_init.init_db()
        session = SessionLocal()
    try:
        touched = seed_defaults(session)
        if owns_session:
            session.commit()
        return len(touched)
    finally:
        if owns_session:
            session.close()


def main() -> int:
    configure_logging("INFO")
    settings = get_settings()
    log.info(
        "seed_default_prompts.start",
        database_url=settings.DATABASE_URL,
        testing=bool(settings.TESTING),
    )
    n = seed()
    # Verify: there should be exactly len(EVENT_TYPES) rows (15 + DEFAULT).
    with SessionLocal() as session:
        rows = session.execute(select(PromptTemplate)).scalars().all()
        event_types = sorted(r.event_type for r in rows)
    print(f"Touched {n} templates.")
    print(f"Total prompt_templates rows: {len(rows)}")
    print(f"Event types: {event_types}")
    expected = sorted(EVENT_TYPES)
    if event_types != expected:
        log.error(
            "seed_default_prompts.mismatch",
            expected=expected,
            actual=event_types,
        )
        return 1
    log.info("seed_default_prompts.done", count=len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
