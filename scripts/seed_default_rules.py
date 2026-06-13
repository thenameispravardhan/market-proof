"""Seed the default signal-rule set onto the default strategy.

Idempotent: re-running updates existing rules in place (keyed by name)
rather than duplicating. Without these the bot HOLDs every analysis.

Usage:
    .venv/Scripts/python.exe scripts/seed_default_rules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analyzer.default_rules import (  # noqa: E402
    seed_default_paper_account,
    seed_default_rules,
)
from app.db import init as db_init  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging, get_logger  # noqa: E402

log = get_logger("scripts.seed_default_rules")


def seed(session=None) -> int:
    """Seed default rules. Returns rows touched. Opens + commits its own
    session when none is provided; otherwise the caller owns the txn."""
    owns_session = session is None
    if owns_session:
        db_init.init_db()
        session = SessionLocal()
    try:
        touched = seed_default_rules(session)
        seed_default_paper_account(session)
        if owns_session:
            session.commit()
        return touched
    finally:
        if owns_session:
            session.close()


def main() -> int:
    configure_logging("INFO")
    n = seed()
    log.info("seed_default_rules.done", touched=n)
    print(f"Seeded/updated {n} default signal rule(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
