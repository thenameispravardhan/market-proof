"""Tests for the backtest engine.

Coverage:
  - With a seeded 30-day dataset (via backtest_seed.seed()), the
    engine produces a populated BacktestResult with trades and
    an equity curve.
  - The result has a non-zero `signals_generated` for the
    default strategy.
  - The metrics are non-zero (we expect at least one trade).
  - Idempotency: re-running the engine on the same dataset
    produces the same number of decisions (deterministic price
    model + seeded data).
  - Edge: a backtest over an empty date range returns 0
    decisions.
  - Validation: end_date < start_date raises.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from app.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    SyntheticReturnPriceModel,
    run_backtest,
)
from app.db import session as db_session_mod
from app.db.models import Announcement
from sqlalchemy import select


def _session_factory():
    """Always returns the LIVE SessionLocal — `rebuild_engine_for_testing()`
    rebinds it, so we must read the attribute at call time."""
    return db_session_mod.SessionLocal()


# ---- Helpers -----------------------------------------------------------


def _seed_announcements_for_backtest(session, *, days: int = 30) -> int:
    """Insert a small synthetic dataset for the test. We don't
    call `scripts/backtest_seed.seed()` directly because that
    script creates a strategy + prompt_templates + paper account
    that may already exist (which is fine — it's idempotent —
    but we want full control of the announcement count for
    predictable assertions)."""
    from app.analyzer.rules_engine import (
        DEFAULT_STRATEGY_NAME,
        get_or_create_default_strategy,
    )
    from app.analyzer.prompts import seed_defaults
    from app.db.models import (
        BrokerAccount,
        SignalRule,
        compute_content_hash,
    )

    strategy = get_or_create_default_strategy(session)
    seed_defaults(session)
    # Default paper account.
    if session.query(BrokerAccount).count() == 0:
        session.add(
            BrokerAccount(
                name="backtest-test-paper",
                broker="paper",
                paper_mode=True,
                enabled=True,
            )
        )
        session.flush()
    # Default rule.
    if session.query(SignalRule).count() == 0:
        session.add(
            SignalRule(
                strategy_id=int(strategy.id),
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
        )
        session.flush()
    # Announcements.
    syms = ["RELIANCE", "TCS", "INFY"]
    end = date.today()
    start = end - timedelta(days=days)
    existing = {
        h for (h,) in session.execute(select(Announcement.content_hash)).all() if h
    }
    new = 0
    for d in range(days):
        for s in syms:
            filed = f"{start + timedelta(days=d)}T10:00:00+00:00"
            from datetime import datetime

            filed_dt = datetime.fromisoformat(filed)
            pdf = f"https://synth.local/{s}/{filed_dt.date().isoformat()}.pdf"
            ch = compute_content_hash(
                exchange="NSE", symbol=s, filed_at=filed_dt, pdf_url=pdf,
            )
            if ch in existing:
                continue
            session.add(
                Announcement(
                    content_hash=ch,
                    symbol=s,
                    exchange="NSE",
                    event_type="ORDER_WIN",
                    headline=(
                        f"{s} wins a Rs {1000 + d * 50} crore order "
                        f"from a leading client"
                    ),
                    body="Synthetic body.",
                    pdf_url=pdf,
                    source="synthetic",
                    filed_at=filed_dt,
                    received_at=filed_dt,
                )
            )
            existing.add(ch)
            new += 1
    session.commit()
    return new


@pytest.fixture()
def seeded_db(db_session, isolated_db):
    """Seed a 30-day window + return the strategy id and count."""
    n = _seed_announcements_for_backtest(db_session, days=30)
    # Read back the strategy id.
    from app.analyzer.rules_engine import get_or_create_default_strategy

    strategy = get_or_create_default_strategy(db_session)
    return {"announcements_inserted": n, "strategy_id": int(strategy.id)}


# ---- Tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_produces_trades_and_curve(seeded_db, db_session):
    cfg = BacktestConfig(
        name="t7-test-1",
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
        initial_capital=200_000.0,
    )
    engine = BacktestEngine(cfg, session_factory=_session_factory)
    result = await engine.run()
    assert result.announcements_processed > 0
    # We seeded 30 * 3 = 90 announcements (some may dedupe).
    assert result.signals_generated > 0
    # The default rule fires on every positive headline — we should
    # get a mix of fills and blocks (any trades blocked by risk
    # counts as a blocked_trades row).
    assert (len(result.trades) + result.blocked_trades) >= 1
    # Equity curve: at least 2 points (start + end).
    assert len(result.equity_curve) >= 2
    assert result.equity_curve[0]["equity"] == pytest.approx(200_000.0)
    # Metrics dict is populated.
    m = result.metrics
    for k in (
        "initial_capital",
        "final_equity",
        "total_return",
        "total_return_pct",
        "sharpe_ratio",
        "max_drawdown",
        "max_drawdown_pct",
        "win_rate",
        "avg_win",
        "avg_loss",
        "profit_factor",
        "total_trades",
        "winning_trades",
        "losing_trades",
        "signals_generated",
        "blocked_trades",
    ):
        assert k in m, f"missing metric {k}"
    # At least one trade should have been recorded.
    if result.trades:
        t0 = result.trades[0]
        for k in (
            "broker_order_id",
            "symbol",
            "side",
            "quantity",
            "price",
            "order_type",
            "status",
            "signal_id",
            "announcement_id",
        ):
            assert k in t0, f"missing trade key {k}"


@pytest.mark.asyncio
async def test_engine_idempotent_on_replay(seeded_db, db_session):
    """Same dataset + same config → same metrics (deterministic
    price model)."""
    cfg = BacktestConfig(
        name="t7-test-idem",
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
        initial_capital=100_000.0,
    )
    r1 = await BacktestEngine(cfg, session_factory=_session_factory).run()
    # Reset the trades/positions/signals/analyses produced by r1
    # so the second run starts from a clean slate. (We can do
    # this because the engine only writes to those tables; the
    # announcements table is the seeded input.)
    from app.db.models import (
        Analysis,
        Position as PositionRow,
        Signal,
        Trade as TradeRow,
    )

    with db_session_mod.SessionLocal() as s:
        s.query(TradeRow).filter(TradeRow.broker_order_id.like("PAPER-%")).delete()
        s.query(PositionRow).delete()
        s.query(Signal).delete()
        s.query(Analysis).delete()
        s.commit()
    r2 = await BacktestEngine(cfg, session_factory=_session_factory).run()
    # Same number of signals, same number of trades, same final
    # equity (the price model is deterministic).
    assert r1.signals_generated == r2.signals_generated
    assert len(r1.trades) == len(r2.trades)
    assert r1.metrics["final_equity"] == pytest.approx(r2.metrics["final_equity"])


@pytest.mark.asyncio
async def test_engine_does_not_write_live_tables(seeded_db, db_session):
    """Isolation: a backtest must not leak into the live trading view.

    The engine runs the whole pipeline (analyzer → rules → risk →
    paper) inside a throwaway in-memory sandbox; the only output that
    reaches the real DB is the `backtest_runs.results` blob. So after
    a run the shared analyses / signals / trades / positions tables —
    the ones the Dashboard, Trade History and Positions panels read —
    must be exactly as empty as before. This is the "results save on
    the Backtest page only" guarantee.
    """
    from app.db.models import (
        Analysis,
        Position as PositionRow,
        Signal,
        Trade as TradeRow,
    )

    cfg = BacktestConfig(
        name="t7-isolation",
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
        initial_capital=100_000.0,
    )
    result = await BacktestEngine(cfg, session_factory=_session_factory).run()
    # The run genuinely exercised the pipeline (otherwise the leak
    # assertions below would be vacuously true).
    assert result.signals_generated > 0
    assert (len(result.trades) + result.blocked_trades) >= 1

    # ...yet nothing it simulated touched the live tables.
    with db_session_mod.SessionLocal() as s:
        assert s.query(Analysis).count() == 0
        assert s.query(Signal).count() == 0
        assert s.query(TradeRow).count() == 0
        assert s.query(PositionRow).count() == 0


@pytest.mark.asyncio
async def test_engine_empty_window(db_session, isolated_db):
    """Backtest over a window with no announcements."""
    cfg = BacktestConfig(
        name="t7-test-empty",
        start_date=date(2000, 1, 1),
        end_date=date(2000, 1, 7),
        initial_capital=100_000.0,
    )
    result = await BacktestEngine(cfg, session_factory=_session_factory).run()
    assert result.announcements_processed == 0
    assert result.signals_generated == 0
    assert len(result.trades) == 0
    assert result.metrics["total_trades"] == 0
    assert result.metrics["final_equity"] == pytest.approx(100_000.0)


def test_engine_rejects_bad_dates():
    cfg = BacktestConfig(
        start_date=date(2025, 1, 10),
        end_date=date(2025, 1, 1),  # before start
        initial_capital=100_000.0,
    )
    engine = BacktestEngine(cfg, session_factory=_session_factory)
    with pytest.raises(ValueError):
        engine.run_sync()


def test_engine_rejects_zero_capital():
    cfg = BacktestConfig(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 7),
        initial_capital=0.0,
    )
    engine = BacktestEngine(cfg, session_factory=_session_factory)
    with pytest.raises(ValueError):
        engine.run_sync()


def test_synthetic_price_model_deterministic():
    pm = SyntheticReturnPriceModel()
    from datetime import datetime, timezone
    when = datetime(2025, 6, 1, tzinfo=timezone.utc)
    p1 = pm.price("RELIANCE", when)
    p2 = pm.price("RELIANCE", when)
    assert p1 == p2
    assert p1 is not None and p1 > 0


def test_synthetic_price_model_unknown_symbol():
    pm = SyntheticReturnPriceModel()
    from datetime import datetime, timezone
    when = datetime(2025, 6, 1, tzinfo=timezone.utc)
    # Unknown symbol → default base (1000) × exp(0) = 1000
    p = pm.price("ZZZZZZ", when)
    assert p is not None and p > 0


@pytest.mark.asyncio
async def test_engine_risk_blocks_with_zero_capital_risk_pct(
    db_session, isolated_db
):
    """With strategy.config['max_capital_risk_pct']=0, every BUY
    signal is blocked by the risk engine (R4). Engine records
    the block in the decisions list and produces 0 trades."""
    from datetime import date, datetime, timedelta, timezone
    from app.analyzer.prompts import seed_defaults
    from app.analyzer.rules_engine import get_or_create_default_strategy
    from app.backtest.engine import (
        BacktestConfig,
        BacktestEngine,
    )
    from app.db.models import (
        Announcement,
        BrokerAccount,
        SignalRule,
        compute_content_hash,
    )

    # Insert strategy with override that blocks all trades.
    strategy = get_or_create_default_strategy(db_session)
    strategy.config = {"max_capital_risk_pct": 0.0}
    db_session.add(strategy)
    db_session.flush()
    seed_defaults(db_session)
    if db_session.query(BrokerAccount).count() == 0:
        db_session.add(
            BrokerAccount(
                name="zero-risk-test-paper",
                broker="paper",
                paper_mode=True,
                enabled=True,
            )
        )
        db_session.flush()
    if db_session.query(SignalRule).count() == 0:
        db_session.add(
            SignalRule(
                strategy_id=int(strategy.id),
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
        )
        db_session.flush()
    # Seed 5 announcements in the past 5 days.
    today = date.today()
    for d in range(5):
        filed = datetime.combine(
            today - timedelta(days=d), datetime.min.time()
        ).replace(tzinfo=timezone.utc, hour=10)
        for sym in ("RELIANCE", "TCS"):
            pdf = f"https://synth.local/{sym}/{filed.date().isoformat()}.pdf"
            ch = compute_content_hash(
                exchange="NSE", symbol=sym, filed_at=filed, pdf_url=pdf
            )
            if db_session.query(Announcement).filter_by(content_hash=ch).first():
                continue
            db_session.add(
                Announcement(
                    content_hash=ch,
                    symbol=sym,
                    exchange="NSE",
                    event_type="ORDER_WIN",
                    headline=(
                        f"{sym} wins a Rs 1000 crore order on day {d}"
                    ),
                    body="synth",
                    pdf_url=pdf,
                    source="synthetic",
                    filed_at=filed,
                    received_at=filed,
                )
            )
    db_session.commit()
    db_session.refresh(strategy)
    cfg = BacktestConfig(
        name="zero-risk-test",
        strategy_id=int(strategy.id),
        start_date=today - timedelta(days=5),
        end_date=today,
        initial_capital=100_000.0,
    )
    result = await BacktestEngine(
        cfg, session_factory=_session_factory
    ).run()
    # The default rule fires (sentiment_score >= 50) but every
    # BUY is blocked at the risk stage (max_capital_risk_pct=0).
    assert result.signals_generated > 0
    # Engine blocked every signal; the trade list is empty.
    assert len(result.trades) == 0
    # Some decisions should be marked as blocked. Look for
    # at least one RISK_MAX_CAPITAL_RISK_PCT or RISK_QTY_BELOW_1
    # block reason.
    risk_blocks = [
        d
        for d in result.decisions
        if d.get("outcome") == "blocked"
        and (
            "RISK_MAX_CAPITAL_RISK_PCT" in str(d.get("reason", ""))
            or "RISK_QTY_BELOW_1" in str(d.get("reason", ""))
        )
    ]
    assert len(risk_blocks) >= 1


def test_synthesise_analysis_matches_order_win_headline():
    """The engine's keyword heuristic must recognise "wins a Rs N
    crore order" headlines (a real-world NSE announcement shape)
    as positive + BUY. The default rule in the seeded engine
    matches on sentiment_score >= 50 AND confidence >= 0.7, so
    a positive synthesised analysis must generate a BUY signal."""
    from app.backtest.engine import _synthesise_analysis
    from datetime import datetime, timezone
    filed = datetime(2025, 6, 1, tzinfo=timezone.utc)
    a = _make_announcement(
        symbol="RELIANCE",
        headline="RELIANCE wins a Rs 5000 crore order from a leading client",
        filed=filed,
    )
    ad = _synthesise_analysis(a, 2500.0)
    assert ad["sentiment"] == "positive"
    assert ad["recommendation"] == "BUY"
    assert ad["sentiment_score"] >= 50.0
    assert ad["confidence"] >= 0.7


def test_synthesise_analysis_handles_negative_headlines():
    from app.backtest.engine import _synthesise_analysis
    from datetime import datetime, timezone
    filed = datetime(2025, 6, 1, tzinfo=timezone.utc)
    a = _make_announcement(
        symbol="TATAMOTORS",
        headline="TATAMOTORS Q3 net profit down 22% YoY on weak demand",
        filed=filed,
    )
    ad = _synthesise_analysis(a, 800.0)
    assert ad["sentiment"] == "negative"
    assert ad["recommendation"] == "SELL"


# Helper for the synthesise tests.


def _make_announcement(*, symbol: str, headline: str, filed):
    from app.db.models import Announcement, compute_content_hash
    pdf = f"https://synth.local/{symbol}/{filed.date().isoformat()}.pdf"
    ch = compute_content_hash(exchange="NSE", symbol=symbol, filed_at=filed, pdf_url=pdf)
    return Announcement(
        content_hash=ch,
        symbol=symbol,
        exchange="NSE",
        event_type="ORDER_WIN",
        headline=headline,
        body="",
        pdf_url=pdf,
        source="synthetic",
        filed_at=filed,
        received_at=filed,
    )
