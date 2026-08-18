"""Observability + strategy-intelligence suite.

Covers the roadmap features:
  - GET /api/metrics/pipeline-latency (percentiles, fast/LLM split,
    news-age histogram, deadline hit rate, sparkline series)
  - the daily health report (compile + format + manual send)
  - the performance-weighted sizer (tiers + risk-engine integration)
  - the sentiment decay curve (age-based target multiplier)
  - GET /api/outcomes (+ summary + CSV export)
  - circuit-breaker history (trip persistence + API)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.db.models import (
    Analysis,
    Announcement,
    Signal,
    SignalOutcome,
    Trade as TradeRow,
)
from app.execution.manager import Manager
from app.execution.market_data import MarketDataBus
from app.risk import circuit_breakers as cb
from app.risk import perf_sizer
from app.risk.engine import RiskEngine
from app.services import health_report as hr


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- seed helpers ----------------------------------------------------------


_HASH_SEQ = iter(range(10_000))


def seed_announcement(db, *, symbol="RELIANCE", event_type="ORDER_WIN") -> Announcement:
    ann = Announcement(
        symbol=symbol,
        exchange="NSE",
        event_type=event_type,
        headline=f"{symbol} {event_type} filing",
        filed_at=utcnow_naive(),
        content_hash=f"obs-hash-{next(_HASH_SEQ)}",
    )
    db.add(ann)
    db.flush()
    return ann


def seed_analysis(
    db, ann: Announcement, *, model="deepseek-chat", timings=None,
    sentiment_score=80.0, confidence=0.9,
) -> Analysis:
    a = Analysis(
        announcement_id=ann.id,
        model=model,
        sentiment="positive",
        sentiment_score=sentiment_score,
        confidence=confidence,
        recommendation="BUY",
        raw_response={"timings": timings} if timings else {},
    )
    db.add(a)
    db.flush()
    return a


def seed_signal(
    db, analysis: Analysis, *, symbol="RELIANCE", action="BUY",
    status="pending", rationale="",
) -> Signal:
    s = Signal(
        analysis_id=analysis.id,
        symbol=symbol,
        action=action,
        confidence=0.9,
        status=status,
        rationale=rationale,
    )
    db.add(s)
    db.flush()
    return s


def seed_closed_trades(db, event_type: str, results: list[tuple[float, float]]) -> None:
    """One announcement→analysis→signal→exit-trade chain per (pnl, r)."""
    for pnl, r in results:
        ann = seed_announcement(db, event_type=event_type)
        analysis = seed_analysis(db, ann)
        sig = seed_signal(db, analysis, status="closed")
        db.add(
            TradeRow(
                signal_id=sig.id,
                symbol="RELIANCE",
                side="SELL",
                quantity=10,
                price=100.0,
                order_type="market",
                status="filled",
                pnl=pnl,
                r_multiple=r,
                executed_at=utcnow_naive(),
            )
        )
    db.commit()


# =========================================================================
# Pipeline latency metrics
# =========================================================================


def test_pipeline_latency_percentiles_and_split(client, db_session, isolated_db):
    # Two LLM analyses + one fast-track, plus one deadline-blocked signal.
    for total, llm in ((2000.0, 1500.0), (3000.0, 2400.0)):
        ann = seed_announcement(db_session)
        a = seed_analysis(
            db_session, ann,
            timings={
                "pdf_fetch_ms": 300.0, "pdf_extract_ms": 200.0,
                "llm_ms": llm, "total_ms": total, "news_age_s_at_start": 12.0,
            },
        )
        seed_signal(db_session, a)
    ann = seed_announcement(db_session)
    ft = seed_analysis(
        db_session, ann, model="fast-track",
        timings={
            "pdf_fetch_ms": None, "pdf_extract_ms": None,
            "llm_ms": 0.0, "total_ms": 50.0, "news_age_s_at_start": 3.0,
        },
    )
    seed_signal(
        db_session, ft, status="blocked",
        rationale="blocked: pipeline_deadline: 25.0s from filing > 20s limit",
    )
    db_session.commit()

    r = client.get("/api/metrics/pipeline-latency?window=50")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"] == {"total": 3, "fast_track": 1, "llm": 2}
    assert body["fast_track_pct"] == pytest.approx(33.3, abs=0.1)
    assert body["latency"]["total_ms"]["p50"] == pytest.approx(2000.0)
    assert body["latency"]["total_ms"]["p99"] <= 3000.0
    assert body["latency"]["llm_ms"]["count"] == 3
    # News-age histogram: 3.0s lands in the <5 bucket, 12.0s in <30.
    hist = {h["le"]: h["count"] for h in body["news_age_s_at_start"]["histogram"]}
    assert hist[5.0] == 1 and hist[30.0] == 2
    # Deadline hit rate: 1 of the 3 signals was blocked as late.
    assert body["deadline"]["blocked"] == 1
    assert body["deadline"]["total_signals"] == 3
    # Sparkline series oldest→newest with track labels.
    assert len(body["series"]) == 3
    assert body["series"][-1]["track"] == "fast"


def test_pipeline_latency_empty_db(client, isolated_db):
    r = client.get("/api/metrics/pipeline-latency")
    assert r.status_code == 200
    body = r.json()
    assert body["window"] == 0
    assert body["latency"]["total_ms"]["p95"] is None


def test_execution_latency_stage_breakdown(client, db_session, isolated_db):
    """A full Trade→Signal→Analysis→Announcement chain with controlled
    timestamps must yield the right per-layer millisecond deltas."""
    t0 = datetime(2026, 7, 18, 6, 0, 0)  # naive UTC, matches storage
    ann = Announcement(
        symbol="RELIANCE", exchange="NSE", event_type="ORDER_WIN",
        headline="RELIANCE ORDER_WIN filing",
        filed_at=t0,
        received_at=t0 + timedelta(seconds=8),   # detection = 8000 ms
        content_hash="exec-lat-1",
    )
    db_session.add(ann)
    db_session.flush()
    a = Analysis(
        announcement_id=ann.id, model="deepseek-chat",
        sentiment="positive", sentiment_score=80.0, confidence=0.9,
        recommendation="BUY",
        raw_response={"timings": {"total_ms": 2500.0, "llm_ms": 2000.0}},
    )
    db_session.add(a)
    db_session.flush()
    sig = Signal(
        analysis_id=a.id, symbol="RELIANCE", action="BUY",
        confidence=0.9, status="filled",
        created_at=t0 + timedelta(seconds=10),
    )
    db_session.add(sig)
    db_session.flush()
    db_session.add(
        TradeRow(
            signal_id=sig.id, symbol="RELIANCE", side="BUY", quantity=10,
            price=100.0, order_type="market", status="filled",
            created_at=t0 + timedelta(seconds=10, milliseconds=500),  # signal→order = 500 ms
            executed_at=t0 + timedelta(seconds=11),                   # order→fill  = 500 ms
        )
    )
    db_session.commit()

    # A BSE announcement with a faster publish lag — no trade attached;
    # it must still show up in the per-exchange detection race.
    db_session.add(
        Announcement(
            symbol="TCS", exchange="BSE", event_type="ORDER_WIN",
            headline="TCS ORDER_WIN filing",
            filed_at=t0,
            received_at=t0 + timedelta(seconds=3),  # BSE detection = 3000 ms
            content_hash="exec-lat-2",
        )
    )
    db_session.commit()

    r = client.get("/api/metrics/execution-latency?window=50")
    assert r.status_code == 200
    body = r.json()
    assert body["window"] == 1
    stages = body["stages"]
    assert stages["detection_ms"]["p50"] == pytest.approx(8000.0)
    assert stages["analysis_ms"]["p50"] == pytest.approx(2500.0)
    assert stages["signal_to_order_ms"]["p50"] == pytest.approx(500.0)
    assert stages["order_to_fill_ms"]["p50"] == pytest.approx(500.0)
    # Per-trade detail row carries every stage for the table.
    assert body["series"][0]["symbol"] == "RELIANCE"
    assert body["series"][0]["detection_ms"] == pytest.approx(8000.0)
    # Detection race: per-exchange publish-lag percentiles over ALL
    # recent announcements (not just traded ones).
    race = body["detection_by_exchange"]
    assert race["NSE"]["p50"] == pytest.approx(8000.0)
    assert race["BSE"]["p50"] == pytest.approx(3000.0)
    assert body["detection_samples"] == 2
    # monitor_ticks is present (may be empty when no monitor ran in-test).
    assert "monitor_ticks" in body


def test_execution_latency_ignores_unfilled_and_empty(client, db_session, isolated_db):
    """Trades with no executed_at are excluded; an empty result is well-formed."""
    r = client.get("/api/metrics/execution-latency")
    assert r.status_code == 200
    assert r.json()["window"] == 0

    # A placed-but-unfilled trade (executed_at is NULL) must not appear.
    db_session.add(
        TradeRow(
            signal_id=None, symbol="TCS", side="BUY", quantity=1,
            price=100.0, order_type="market", status="placed",
        )
    )
    db_session.commit()
    assert client.get("/api/metrics/execution-latency").json()["window"] == 0


# =========================================================================
# Daily health report
# =========================================================================


def test_health_report_compile_and_send(client, db_session, isolated_db):
    ann = seed_announcement(db_session)
    a = seed_analysis(db_session, ann)
    seed_signal(db_session, a, status="blocked")
    seed_signal(db_session, a, status="filled")
    db_session.add(
        TradeRow(symbol="RELIANCE", side="BUY", quantity=10, price=100.0,
                 order_type="limit", status="filled", pnl=None)
    )
    db_session.add(
        TradeRow(symbol="RELIANCE", side="SELL", quantity=10, price=106.0,
                 order_type="market", status="filled", pnl=60.0)
    )
    db_session.commit()

    r = client.get("/api/metrics/health-report")
    assert r.status_code == 200
    report = r.json()
    assert report["signals_total"] == 2
    assert report["signals_blocked_risk"] == 1
    assert report["trades_executed"] == 1
    assert report["positions_closed"] == 1
    assert report["realized_pnl"] == pytest.approx(60.0)
    assert report["circuit_breaker"] == "OK"

    subject, body = hr.format_report(report)
    assert "Daily health report" in subject
    assert "Trades executed:" in body and "Circuit breaker:" in body

    r2 = client.post("/api/metrics/health-report/send")
    assert r2.status_code == 200
    assert r2.json()["ok"] is True
    assert "Daily health report" in r2.json()["subject"]


def test_health_report_reflects_halt(client, db_session, isolated_db):
    cb.trip_manual_kill(db_session)
    r = client.get("/api/metrics/health-report")
    assert r.status_code == 200
    assert r.json()["circuit_breaker"].startswith("HALTED")
    cb.clear_halt(db_session)


# =========================================================================
# Pre-market preflight
# =========================================================================


def _preflight_env(monkeypatch, *, ai_on: bool, quote):
    """AI toggle + Fyers quote probe, both faked."""
    monkeypatch.setattr(
        hr, "get_settings",
        lambda: SimpleNamespace(AI_ANALYSIS_ENABLED=ai_on),
    )

    async def _fake_quote(_symbol):
        return quote

    import app.api.market as market_mod
    monkeypatch.setattr(market_mod, "fetch_quote", _fake_quote)


@pytest.mark.asyncio
async def test_preflight_flags_dead_token_and_ai_off(monkeypatch):
    """The two silent day-killers: an expired Fyers token (quote probe
    returns None) and AI analysis switched off."""
    _preflight_env(monkeypatch, ai_on=False, quote=None)
    problems = await hr.compile_preflight()
    assert len(problems) == 2
    assert any("AI analysis is OFF" in p for p in problems)
    assert any("NO_LIVE_PRICE" in p for p in problems)


@pytest.mark.asyncio
async def test_preflight_silent_when_healthy(monkeypatch):
    """A healthy morning publishes nothing — an alarm that fires daily
    gets muted."""
    _preflight_env(monkeypatch, ai_on=True, quote={"last_price": 24000.0})
    published: list = []

    async def _spy(channel, payload):
        published.append((channel, payload))
        return 0

    monkeypatch.setattr(hr.event_bus, "publish", _spy)
    svc = hr.HealthReportService()
    assert await svc.run_preflight() == []
    assert published == []


@pytest.mark.asyncio
async def test_preflight_alerts_once_per_trading_day(monkeypatch):
    """Fires on `system.error` when broken, then not again that day; and
    never on a weekend."""
    _preflight_env(monkeypatch, ai_on=True, quote=None)
    published: list = []

    async def _spy(channel, payload):
        published.append((channel, payload))
        return 1

    monkeypatch.setattr(hr.event_bus, "publish", _spy)
    monkeypatch.setattr(
        hr, "get_settings",
        lambda: SimpleNamespace(
            AI_ANALYSIS_ENABLED=True, HEALTH_REPORT_PREFLIGHT_TIME_IST="09:05"
        ),
    )
    svc = hr.HealthReportService()

    # 2026-08-17 is a Monday; 09:10 IST = 03:40 UTC.
    monday = datetime(2026, 8, 17, 3, 40, tzinfo=timezone.utc)
    assert svc._preflight_due(monday) is True
    assert len(await svc.run_preflight(monday)) == 1
    assert published[0][0] == hr.CHANNEL_SYSTEM_ERROR
    assert svc._preflight_due(monday) is False          # already fired today

    # Before the fire time, and on the weekend, it stays quiet.
    svc._last_preflight_date = None
    assert svc._preflight_due(datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)) is False
    assert svc._preflight_due(datetime(2026, 8, 15, 3, 40, tzinfo=timezone.utc)) is False


# =========================================================================
# Performance-weighted sizer
# =========================================================================


def _perf_settings(**over):
    base = dict(
        PERF_SIZER_ENABLED=True, PERF_SIZER_MIN_TRADES=10,
        PERF_SIZER_LOOKBACK_TRADES=200,
        PERF_SIZER_HIGH_WIN_RATE=0.60, PERF_SIZER_HIGH_AVG_R=1.5,
        PERF_SIZER_HIGH_RISK_PCT=1.0,
        PERF_SIZER_LOW_WIN_RATE=0.50, PERF_SIZER_LOW_AVG_R=1.0,
        PERF_SIZER_LOW_RISK_PCT=0.5,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_perf_sizer_high_tier(db_session, isolated_db):
    # 8/12 wins (66.7%) at avg R (8×3 − 4×1)/12 = 1.67 → high tier.
    seed_closed_trades(
        db_session, "ORDER_WIN",
        [(100.0, 3.0)] * 8 + [(-50.0, -1.0)] * 4,
    )
    pct, ctx = perf_sizer.performance_risk_pct(
        db_session, "ORDER_WIN", 0.75, _perf_settings()
    )
    assert pct == pytest.approx(1.0)
    assert ctx is not None and ctx["tier"] == "high"
    assert ctx["trades"] == 12 and ctx["wins"] == 8


def test_perf_sizer_low_tier_on_weak_win_rate(db_session, isolated_db):
    # 5/12 wins (41.7% < 50%) → demote regardless of R.
    seed_closed_trades(
        db_session, "BOARD_MEETING",
        [(100.0, 2.0)] * 5 + [(-50.0, -1.0)] * 7,
    )
    pct, ctx = perf_sizer.performance_risk_pct(
        db_session, "BOARD_MEETING", 0.75, _perf_settings()
    )
    assert pct == pytest.approx(0.5)
    assert ctx is not None and ctx["tier"] == "low"


def test_perf_sizer_needs_min_trades(db_session, isolated_db):
    # 3 closed trades < MIN_TRADES → base %% unchanged, sizer silent.
    seed_closed_trades(db_session, "BUYBACK", [(100.0, 2.0)] * 3)
    pct, ctx = perf_sizer.performance_risk_pct(
        db_session, "BUYBACK", 0.75, _perf_settings()
    )
    assert pct == pytest.approx(0.75)
    assert ctx is None


@pytest.mark.asyncio
async def test_engine_applies_perf_tier(db_session, isolated_db):
    """The risk engine sizes with the tiered %%: identical signal, HIGH-
    tier event → risk_pct 1.0 lands in the decision context and the qty
    scales accordingly."""
    seed_closed_trades(
        db_session, "ORDER_WIN",
        [(100.0, 3.0)] * 8 + [(-50.0, -1.0)] * 4,
    )
    md = MarketDataBus()
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    sig = SimpleNamespace(id=None, symbol="TCS", action="BUY",
                          confidence=0.9, position_size_pct=0.0,
                          strategy_id=None, rationale="")
    # Wide stop keeps the risk-based qty below the single-name notional
    # cap, so the tier ratio is visible in the final quantity.
    base = await engine.evaluate(signal=sig, entry=100.0, stop_loss=90.0,
                                 event_type="BOARD_MEETING")  # no history
    tiered = await engine.evaluate(signal=sig, entry=100.0, stop_loss=90.0,
                                   event_type="ORDER_WIN")
    assert base.approved and tiered.approved
    assert "perf_sizer" not in base.context
    assert tiered.context["perf_sizer"]["tier"] == "high"
    assert tiered.context["sizing"]["max_capital_risk_pct"] == pytest.approx(1.0)
    # 1.0% vs the 0.75% default → ~1.33× the quantity.
    assert tiered.sizing.qty == pytest.approx(base.sizing.qty * (1.0 / 0.75), rel=0.02)


# =========================================================================
# Sentiment decay curve
# =========================================================================


def test_sentiment_decay_multiplier_bands():
    st = SimpleNamespace(
        SENTIMENT_DECAY_ENABLED=True,
        SENTIMENT_DECAY_FULL_SECONDS=2.0,
        SENTIMENT_DECAY_PARTIAL_SECONDS=5.0,
        SENTIMENT_DECAY_PARTIAL_MULT=0.8,
        SENTIMENT_DECAY_STALE_MULT=0.6,
    )
    f = Manager._sentiment_decay_multiplier
    assert f(1.0, st) == 1.0          # fresh → full target
    assert f(3.0, st) == 0.8          # 2–5s → partial decay
    assert f(10.0, st) == 0.6         # stale → diminished edge
    assert f(None, st) == 1.0         # unknown age never guesses
    st_off = SimpleNamespace(SENTIMENT_DECAY_ENABLED=False)
    assert f(60.0, st_off) == 1.0     # toggle respects the invariant


def test_derive_levels_applies_decay(db_session, isolated_db):
    md = MarketDataBus()
    mgr = Manager(risk_engine=RiskEngine(market_data=md, portfolio_value=1e6),
                  market_data=md)
    fresh_sl, fresh_target, fresh_rr, _ = mgr._derive_levels(
        symbol="RELIANCE", entry=100.0, action="BUY",
        event_type=None, news_age_seconds=1.0,
    )
    stale_sl, stale_target, stale_rr, _ = mgr._derive_levels(
        symbol="RELIANCE", entry=100.0, action="BUY",
        event_type=None, news_age_seconds=10.0,
    )
    # The stop is untouched; only the reward expectation shrinks.
    assert stale_sl == pytest.approx(fresh_sl)
    assert stale_rr == pytest.approx(fresh_rr * 0.6)
    assert (stale_target - 100.0) == pytest.approx((fresh_target - 100.0) * 0.6)


# =========================================================================
# Signal outcomes viewer
# =========================================================================


def _seed_outcome(db, sig, *, action="BUY", move_5m=2.5, move_30m=1.0):
    db.add(
        SignalOutcome(
            signal_id=sig.id, symbol=sig.symbol, action=action,
            signal_status=sig.status,
            price_at_signal=100.0,
            price_5m=100.0 * (1 + move_5m / 100),
            price_30m=100.0 * (1 + move_30m / 100),
            move_5m_pct=move_5m, move_30m_pct=move_30m,
            note="ok", completed_at=utcnow_naive(),
        )
    )


def test_outcomes_list_summary_and_csv(client, db_session, isolated_db):
    ann1 = seed_announcement(db_session, event_type="ORDER_WIN")
    a1 = seed_analysis(db_session, ann1, sentiment_score=85.0)
    s1 = seed_signal(db_session, a1, status="filled")
    _seed_outcome(db_session, s1, action="BUY", move_5m=2.5, move_30m=1.2)

    ann2 = seed_announcement(db_session, symbol="TCS", event_type="Q1_RESULTS")
    a2 = seed_analysis(db_session, ann2, sentiment_score=-40.0)
    s2 = seed_signal(db_session, a2, symbol="TCS", action="SELL", status="blocked")
    _seed_outcome(db_session, s2, action="SELL", move_5m=1.0, move_30m=0.5)
    db_session.commit()

    rows = client.get("/api/outcomes").json()
    assert len(rows) == 2
    by_symbol = {r["symbol"]: r for r in rows}
    # BUY that went +2.5% in 5m → the AI was right; trade was taken.
    assert by_symbol["RELIANCE"]["ai_was_correct"] is True
    assert by_symbol["RELIANCE"]["trade_taken"] is True
    assert by_symbol["RELIANCE"]["event_type"] == "ORDER_WIN"
    # SELL that went UP → wrong; blocked signal → not taken.
    assert by_symbol["TCS"]["ai_was_correct"] is False
    assert by_symbol["TCS"]["trade_taken"] is False

    summary = client.get("/api/outcomes/summary").json()
    assert summary["total"] == 2
    by_event = {s["event_type"]: s for s in summary["by_event_type"]}
    assert by_event["ORDER_WIN"]["avg_move_5m_pct"] == pytest.approx(2.5)
    assert by_event["ORDER_WIN"]["accuracy_5m_pct"] == pytest.approx(100.0)
    assert by_event["Q1_RESULTS"]["accuracy_5m_pct"] == pytest.approx(0.0)

    # Event-type filter narrows the list.
    filtered = client.get("/api/outcomes?event_type=ORDER_WIN").json()
    assert len(filtered) == 1 and filtered[0]["symbol"] == "RELIANCE"

    csv_resp = client.get("/api/outcomes/export.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    lines = csv_resp.text.strip().splitlines()
    assert lines[0].startswith("outcome_id,signal_id,symbol,action,event_type")
    assert len(lines) == 3  # header + 2 rows


# =========================================================================
# Circuit-breaker history
# =========================================================================


def test_breaker_trip_writes_history(client, db_session, isolated_db):
    # Arm the anchors, then drop equity 3% (> the 2.5% daily cap).
    cb.evaluate_breakers(db_session, equity=1_000_000.0)
    summary = cb.evaluate_breakers(db_session, equity=968_000.0)
    assert summary["trading_disabled"] is True

    rows = client.get("/api/risk/breaker-history").json()
    types = [r["event_type"] for r in rows]
    assert "BREAKER_DAILY_LOSS" in types
    daily = next(r for r in rows if r["event_type"] == "BREAKER_DAILY_LOSS")
    assert daily["threshold_value"] == pytest.approx(2.5)
    assert daily["current_value"] == pytest.approx(3.2)
    assert "flatten" in daily["action_taken"]
    assert daily["halted"] is True

    # Manual resume is part of the same audit trail.
    cb.clear_halt(db_session)
    rows2 = client.get("/api/risk/breaker-history").json()
    assert rows2[0]["event_type"] == "BREAKER_RESUMED"


def test_manual_kill_writes_history(client, db_session, isolated_db):
    cb.trip_manual_kill(db_session)
    rows = client.get("/api/risk/breaker-history").json()
    assert rows[0]["event_type"] == "BREAKER_MANUAL_KILL"
    assert rows[0]["halted"] is True
    cb.clear_halt(db_session)
