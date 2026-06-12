"""Seed a 30-day synthetic dataset of announcements for backtests.

Inserts:
  - 1 default `strategies` row (idempotent — `name="default"`).
  - 1 default paper `broker_accounts` row.
  - 1 `prompt_templates` row per event type (16 total: 15 + DEFAULT).
  - 1 default `signal_rules` row (BUY on positive sentiment) so the
    rules engine produces real matches for the backtest.
  - N announcements across 30 calendar days, mixing positive /
    neutral / negative / HOLD-prone events for 6 NSE symbols.

Idempotent: the script never duplicates a row. The strategy
lookup is by name; the broker account lookup is by name; the
prompt templates are upserted via the existing `seed_defaults`
helper. The announcements table has a UNIQUE constraint on
`content_hash`; we compute hashes deterministically from
(exchange, symbol, filed_at, pdf_url) so re-running the script
yields zero new rows after the first run.

Usage:
    .venv/Scripts/python.exe scripts/backtest_seed.py
    .venv/Scripts/python.exe scripts/backtest_seed.py --days 60

`--days` overrides the 30-day default. `--reset` wipes
announcements first (useful when the operator wants a clean
30-day window with deterministic ids).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the project root importable when run as a script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app.analyzer.prompts import seed_defaults  # noqa: E402
from app.analyzer.rules_engine import (  # noqa: E402
    DEFAULT_STRATEGY_NAME,
    get_or_create_default_strategy,
)
from app.db import init as db_init  # noqa: E402
from app.db.models import (  # noqa: E402
    Announcement,
    BrokerAccount,
    SignalRule,
    compute_content_hash,
)
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging, get_logger  # noqa: E402

log = get_logger("scripts.backtest_seed")


# 6 NSE symbols across 3 sectors so the rules engine + risk engine
# have something to chew on. Picked to overlap with the
# `DEFAULT_SECTOR_MAP` in `app/risk/engine.py`.
SYMBOLS = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN"]

# (count, headline_template, event_type) — distributed across the
# 30-day window. Headlines that drive a positive/negative view
# are interleaved with neutral ones to exercise every code path.
EVENT_TEMPLATES: list[tuple[str, str, str]] = [
    # event_type, headline_template, default_recommendation hint
    ("ORDER_WIN", "{sym} wins a Rs {amt} crore order from a leading client", "BUY"),
    ("ORDER_WIN", "{sym} secures order worth Rs {amt} crore in the export segment", "BUY"),
    ("ORDER_WIN", "{sym} wins Rs {amt} crore defence order; deliveries start Q2", "BUY"),
    ("DIVIDEND", "{sym} declares interim dividend of Rs {dps} per share", "BUY"),
    ("DIVIDEND", "{sym} announces special dividend; record date next month", "BUY"),
    ("BUYBACK", "{sym} board approves Rs {amt} crore share buyback at Rs {px} floor", "BUY"),
    ("ACQUISITION", "{sym} to acquire 51% stake in a fintech for Rs {amt} crore", "BUY"),
    ("MERGER", "{sym} scheme of arrangement with subsidiary cleared by NCLT", "BUY"),
    ("Q1_RESULTS", "{sym} Q1 net profit up 18% YoY at Rs {amt} crore", "BUY"),
    ("Q2_RESULTS", "{sym} Q2 results: revenue at Rs {amt} crore, in line with estimates", "BUY"),
    ("Q3_RESULTS", "{sym} Q3 net profit down 22% YoY on weak demand", "SELL"),
    ("Q4_RESULTS", "{sym} Q4 results miss; PAT at Rs {amt} crore vs est Rs {est} crore", "SELL"),
    ("ANNUAL_RESULTS", "{sym} FY results: revenue Rs {amt} crore, EBITDA margin {mgn}%", "BUY"),
    ("BOARD_MEETING", "{sym} intimation of board meeting to consider fund raise", "HOLD"),
    ("OTHER", "{sym} press release: update on promoter shareholding", "HOLD"),
    ("OTHER", "{sym} clarifies media speculation on debt restructuring", "HOLD"),
]


# Paper broker account name (used by the backtest routing).
DEFAULT_PAPER_ACCOUNT_NAME = "backtest-paper-default"


def _make_announcement(
    *,
    symbol: str,
    exchange: str,
    event_type: str,
    headline: str,
    filed_at: datetime,
) -> Announcement:
    """Build an Announcement row with a deterministic content_hash
    so re-runs are idempotent."""
    pdf_url = f"https://synth.local/{exchange.lower()}/{symbol}/{filed_at.date().isoformat()}.pdf"
    ch = compute_content_hash(
        exchange=exchange,
        symbol=symbol,
        filed_at=filed_at,
        pdf_url=pdf_url,
    )
    return Announcement(
        content_hash=ch,
        symbol=symbol,
        exchange=exchange,
        event_type=event_type,
        headline=headline,
        body=(
            f"{headline}. Synthetic body for the seeded announcement. "
            f"This is a test record created by scripts/backtest_seed.py."
        ),
        pdf_url=pdf_url,
        source="synthetic",
        filed_at=filed_at,
        received_at=filed_at,
    )


def _seed_announcements(session, *, days: int, per_day: int = 4) -> int:
    """Insert `days * per_day` synthetic announcements.

    Spread roughly evenly across the window. Returns the number
    of NEW rows inserted (skips duplicates via content_hash).
    """
    end = datetime.now(timezone.utc).replace(hour=15, minute=30, second=0, microsecond=0)
    start = end - timedelta(days=days)
    span_s = (end - start).total_seconds()

    # Build a deterministic per-day template: cycle through
    # SYMBOLS * EVENT_TEMPLATES. This gives a mix of BUY-prone,
    # SELL-prone, and HOLD-prone headlines over the window.
    new_rows = 0
    existing_hashes = {
        h for (h,) in session.execute(select(Announcement.content_hash)).all() if h
    }
    plan: list[tuple[datetime, str, str, str]] = []
    n = days * per_day
    for i in range(n):
        t_frac = i / max(1, n - 1)
        when = start + timedelta(seconds=span_s * t_frac)
        sym = SYMBOLS[i % len(SYMBOLS)]
        ev_t, headline_t, _hint = EVENT_TEMPLATES[i % len(EVENT_TEMPLATES)]
        # Vary the numbers a bit to make the equity curve
        # non-trivial.
        amt = 100 + (i * 37 % 9500)
        dps = round(1.0 + (i * 13 % 50), 2)
        px = 100 + (i * 53 % 4500)
        mgn = 10 + (i * 7 % 35)
        est = 100 + (i * 41 % 8000)
        headline = headline_t.format(sym=sym, amt=amt, dps=dps, px=px, mgn=mgn, est=est)
        plan.append((when, sym, ev_t, headline))
    for when, sym, ev_t, headline in plan:
        ann = _make_announcement(
            symbol=sym,
            exchange="NSE",
            event_type=ev_t,
            headline=headline,
            filed_at=when,
        )
        if ann.content_hash in existing_hashes:
            continue
        session.add(ann)
        session.flush()
        existing_hashes.add(ann.content_hash)
        new_rows += 1
    return new_rows


def _ensure_broker_account(session) -> BrokerAccount:
    """Create the default paper broker account if it doesn't exist."""
    acc = (
        session.query(BrokerAccount)
        .filter_by(name=DEFAULT_PAPER_ACCOUNT_NAME)
        .one_or_none()
    )
    if acc is not None:
        return acc
    acc = BrokerAccount(
        name=DEFAULT_PAPER_ACCOUNT_NAME,
        broker="paper",
        paper_mode=True,
        enabled=True,
    )
    session.add(acc)
    session.flush()
    return acc


def _ensure_default_rule(session, strategy) -> SignalRule:
    """Insert a single default rule if no rules exist for the strategy.

    The rule: BUY when sentiment_score >= 50 AND confidence >= 0.7.
    This gives the backtest a mix of matches + HOLDs (for the
    SELL/HOLD headlines that don't match).
    """
    existing = (
        session.query(SignalRule).filter_by(strategy_id=strategy.id).first()
    )
    if existing is not None:
        return existing
    rule = SignalRule(
        strategy_id=strategy.id,
        name="default-buy-positive",
        priority=10,
        conditions={
            "all_of": [
                {"field": "sentiment_score", "op": ">=", "value": 50.0},
                {"field": "confidence", "op": ">=", "value": 0.7},
            ],
        },
        action="BUY",
        action_params={"position_size_pct": 5.0},
        enabled=True,
    )
    session.add(rule)
    session.flush()
    return rule


def seed(
    *,
    days: int = 30,
    per_day: int = 4,
    reset_announcements: bool = False,
) -> dict[str, int]:
    """Seed the DB. Returns a count summary as a dict."""
    db_init.init_db()
    session = SessionLocal()
    summary: dict[str, int] = {
        "strategy_id": 0,
        "broker_account_id": 0,
        "prompt_templates": 0,
        "signal_rules": 0,
        "announcements": 0,
    }
    try:
        strategy = get_or_create_default_strategy(session)
        summary["strategy_id"] = int(strategy.id)
        acc = _ensure_broker_account(session)
        summary["broker_account_id"] = int(acc.id)
        # Prompt templates (idempotent via upsert).
        templates = seed_defaults(session)
        summary["prompt_templates"] = len(templates)
        # Default rule.
        rule = _ensure_default_rule(session, strategy)
        summary["signal_rules"] = 1
        # Announcements.
        if reset_announcements:
            session.query(Announcement).delete()
            session.flush()
        new_ann = _seed_announcements(session, days=days, per_day=per_day)
        summary["announcements"] = new_ann
        session.commit()
        return summary
    finally:
        session.close()


def main() -> int:
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="Seed a backtest dataset.")
    parser.add_argument("--days", type=int, default=30, help="calendar days of data")
    parser.add_argument(
        "--per-day", type=int, default=4, help="announcements per day per symbol-bucket"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="wipe existing announcements before seeding",
    )
    args = parser.parse_args()
    log.info("backtest_seed.start", days=args.days, per_day=args.per_day, reset=args.reset)
    summary = seed(days=args.days, per_day=args.per_day, reset_announcements=args.reset)
    print(f"Strategy id: {summary['strategy_id']}")
    print(f"Broker account id: {summary['broker_account_id']}")
    print(f"Prompt templates touched: {summary['prompt_templates']}")
    print(f"Default signal rules: {summary['signal_rules']}")
    print(f"New announcements inserted: {summary['announcements']}")
    log.info("backtest_seed.done", **summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
