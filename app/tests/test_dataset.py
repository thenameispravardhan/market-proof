"""Dataset builder + /api/dataset suite.

Covers:
  - compute_reaction_features: alignment, early features, horizon
    targets, direction-awareness, partial/no-candle statuses,
    look-ahead-safe key split
  - context features: session clock, NIFTY concurrent move, excess ret
  - DatasetBuilder.run_once: enrichment, retry accounting, too_old,
    shadow rows (analyzed-but-never-signaled announcements)
  - GET /api/dataset/columns, /api/dataset (filters + column selection
    + row sources), /stats, /export (csv + jsonl + dedup + time split),
    /health (null rates + leakage flags), POST /backfill
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import (
    Analysis,
    Announcement,
    DatasetFeature,
    Signal,
    SignalOutcome,
    Trade as TradeRow,
)
from app.services.dataset_builder import (
    DatasetBuilder,
    compute_index_context,
    compute_reaction_features,
    compute_time_context,
)


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- synthetic candles ------------------------------------------------------


def make_candles(
    signal_epoch: int,
    *,
    pre_minutes: int = 10,
    post_minutes: int = 15,
    base: float = 100.0,
    per_minute_move: float = 0.5,
    volume: float = 1000.0,
    post_volume: float = 5000.0,
) -> list[list[float]]:
    """1-min candles: flat at `base` before the signal, then trending
    `per_minute_move` per minute after it (positive = up)."""
    m0 = signal_epoch - (signal_epoch % 60)
    candles = []
    for k in range(-pre_minutes, post_minutes + 1):
        if k < 0:
            o = h = l = c = base  # noqa: E741
            v = volume
        else:
            o = base + per_minute_move * k
            c = base + per_minute_move * (k + 1)
            h, l = max(o, c) + 0.05, min(o, c) - 0.05  # noqa: E741
            v = post_volume
        candles.append([m0 + k * 60, o, h, l, c, v])
    return candles


SIGNAL_EPOCH = 1_700_000_045  # arbitrary, mid-minute


# ---- compute_reaction_features ---------------------------------------------


def test_reaction_features_uptrend_complete():
    candles = make_candles(SIGNAL_EPOCH, per_minute_move=0.5)
    feats, status = compute_reaction_features(
        candles, SIGNAL_EPOCH, direction=1, baseline_price=100.0
    )
    assert status == "complete"
    assert feats is not None
    # trajectory: close of minute k = 100 + 0.5*(k+1)
    assert feats["price_1m"] == pytest.approx(101.0)
    assert feats["price_5m"] == pytest.approx(103.0)
    assert feats["ret_1m_pct"] == pytest.approx(1.0)
    assert feats["ret_5m_pct"] == pytest.approx(3.0)
    # FULL minute-by-minute trajectory (the mid-window T+6..T+14)
    assert feats["features_version"] == 2
    assert feats["price_10m"] == pytest.approx(105.5)  # 100 + 0.5*11
    assert feats["ret_10m_pct"] == pytest.approx(5.5)
    assert feats["price_14m"] == pytest.approx(107.5)
    # every minute 1..15 is present
    assert all(f"price_{m}m" in feats for m in range(1, 16))
    assert all(f"ret_{m}m_pct" in feats for m in range(1, 16))
    # targets
    assert feats["price_15m"] == pytest.approx(108.0)
    assert feats["ret_15m_pct"] == pytest.approx(8.0)
    assert feats["high_15m"] == pytest.approx(108.05)
    assert feats["label_15m"] == "UP"
    assert feats["mfe_15m_pct"] == pytest.approx(8.05)
    assert feats["time_to_peak_min"] == 15
    # reaction speed: minute-1 high (101.05) is the first ≥1% touch;
    # 1-min granularity reports the upper bound (k+1)*60 = 120s
    assert feats["time_to_1pct_5m_s"] == 120
    assert feats["hit_1pct_within_5m"] is True
    # volume surge: 5000 post vs 1000 pre
    assert feats["volume_surge_1m"] == pytest.approx(5.0)
    # pre-news was flat
    assert feats["pre_move_5m_pct"] == pytest.approx(0.0)


def test_reaction_features_direction_aware_for_sell():
    candles = make_candles(SIGNAL_EPOCH, per_minute_move=-0.5)
    feats, status = compute_reaction_features(
        candles, SIGNAL_EPOCH, direction=-1, baseline_price=100.0
    )
    assert status == "complete"
    # A falling price is FAVORABLE for a SELL: MFE positive, label DOWN.
    assert feats["ret_15m_pct"] == pytest.approx(-8.0)
    assert feats["label_15m"] == "DOWN"
    assert feats["mfe_15m_pct"] > 0
    assert feats["mae_15m_pct"] <= 0.1
    assert feats["hit_1pct_within_5m"] is True


def test_reaction_features_flat_label_and_no_spike():
    candles = make_candles(SIGNAL_EPOCH, per_minute_move=0.01)
    feats, status = compute_reaction_features(
        candles, SIGNAL_EPOCH, baseline_price=100.0, flat_threshold_pct=0.3
    )
    assert status == "complete"
    assert feats["label_15m"] == "FLAT"
    assert feats["time_to_1pct_5m_s"] is None
    assert feats["hit_1pct_within_5m"] is False


def test_reaction_features_partial_without_horizon_candle():
    # Only 6 post-signal minutes (signal near market close).
    candles = make_candles(SIGNAL_EPOCH, post_minutes=6)
    feats, status = compute_reaction_features(candles, SIGNAL_EPOCH)
    assert status == "partial"
    assert feats["price_5m"] is not None
    assert feats["price_15m"] is None
    assert feats["label_15m"] is None


def test_reaction_features_no_candles():
    feats, status = compute_reaction_features([], SIGNAL_EPOCH)
    assert status == "no_candles"
    assert feats is None
    # Candles that don't cover the signal minute are equally useless.
    far_away = make_candles(SIGNAL_EPOCH - 7200, post_minutes=5, pre_minutes=0)
    feats, status = compute_reaction_features(far_away, SIGNAL_EPOCH)
    assert status == "no_candles"


def test_reaction_features_baseline_fallback_to_open():
    candles = make_candles(SIGNAL_EPOCH, per_minute_move=0.5)
    feats, status = compute_reaction_features(
        candles, SIGNAL_EPOCH, baseline_price=None
    )
    assert status == "complete"
    assert feats["baseline_price"] == pytest.approx(100.0)


def test_time_context_ist_clock():
    # 04:16 UTC = 09:46 IST → 31 minutes after the 09:15 open.
    anchor = datetime(2026, 7, 8, 4, 16)  # a Wednesday
    ctx = compute_time_context(anchor)
    assert ctx["minute_since_open"] == 31
    assert ctx["day_of_week"] == 2


def test_index_context_alignment():
    idx = make_candles(SIGNAL_EPOCH, per_minute_move=0.2, base=25000.0)
    ctx = compute_index_context(idx, SIGNAL_EPOCH)
    # index close at k = 25000 + 0.2*(k+1), open0 = 25000
    assert ctx["nifty_ret_1m_pct"] == pytest.approx(0.4 / 25000 * 100, rel=1e-3)
    assert ctx["nifty_ret_5m_pct"] == pytest.approx(1.2 / 25000 * 100, rel=1e-3)
    assert compute_index_context([], SIGNAL_EPOCH) == {
        "nifty_ret_1m_pct": None,
        "nifty_ret_5m_pct": None,
    }


# ---- DatasetBuilder ---------------------------------------------------------


def seed_outcome(
    db,
    *,
    symbol="RELIANCE",
    action="BUY",
    minutes_ago=20,
    price=100.0,
    signal_id=None,
    created_at=None,
) -> SignalOutcome:
    row = SignalOutcome(
        signal_id=signal_id,
        symbol=symbol,
        action=action,
        signal_status="pending",
        price_at_signal=price,
        created_at=created_at or (utcnow_naive() - timedelta(minutes=minutes_ago)),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_builder_enriches_pending_outcome(db_session, isolated_db):
    outcome = seed_outcome(db_session)
    epoch = int(outcome.created_at.replace(tzinfo=timezone.utc).timestamp())
    candles = make_candles(epoch, per_minute_move=0.5)

    async def candle_fn(symbol, signal_time):
        assert symbol == "RELIANCE"
        return candles

    builder = DatasetBuilder(candle_fn=candle_fn)
    counts = await builder.run_once()
    assert counts["processed"] == 1
    assert counts["complete"] == 1

    row = (
        db_session.query(DatasetFeature)
        .filter(DatasetFeature.outcome_id == outcome.id)
        .one()
    )
    assert row.status == "complete"
    assert row.features["label_15m"] == "UP"
    assert row.features["price_1m"] == pytest.approx(101.0)

    # A second pass finds nothing left to do.
    counts2 = await builder.run_once()
    assert counts2["processed"] == 0


@pytest.mark.asyncio
async def test_builder_retries_no_candles_then_stops(db_session, isolated_db):
    seed_outcome(db_session)

    async def no_candles(symbol, signal_time):
        return []

    builder = DatasetBuilder(candle_fn=no_candles)
    for expected_attempts in (1, 2, 3):
        counts = await builder.run_once()
        assert counts["processed"] == 1
        row = db_session.query(DatasetFeature).one()
        db_session.refresh(row)
        assert row.status == "no_candles"
        assert row.attempts == expected_attempts
    # Attempts exhausted (DATASET_MAX_ATTEMPTS=3) — no more retries.
    counts = await builder.run_once()
    assert counts["processed"] == 0


@pytest.mark.asyncio
async def test_builder_skips_fresh_and_marks_too_old(db_session, isolated_db):
    seed_outcome(db_session, minutes_ago=2)  # window not elapsed yet
    old = seed_outcome(db_session, symbol="TCS", minutes_ago=60 * 24 * 40)

    async def candle_fn(symbol, signal_time):
        return []

    builder = DatasetBuilder(candle_fn=candle_fn)
    counts = await builder.run_once()
    assert counts["processed"] == 1
    assert counts["too_old"] == 1
    row = db_session.query(DatasetFeature).one()
    assert row.outcome_id == old.id
    assert row.status == "too_old"


def test_reaction_features_sparse_symbol_asof_fill():
    """Illiquid BSE names trade a few times an hour — 'price at T+k' is
    the last trade at-or-before minute k, bounded by the session close."""
    m0 = SIGNAL_EPOCH - (SIGNAL_EPOCH % 60)

    def candle(offset, close, vol=50.0):
        return [m0 + offset * 60, close, close + 0.02, close - 0.02, close, vol]

    # trades at T-4, T+2 and T+9 only; nothing at the signal minute
    candles = [candle(-4, 100.0), candle(2, 101.0), candle(9, 103.0)]
    feats, status = compute_reaction_features(
        candles,
        SIGNAL_EPOCH,
        baseline_price=None,
        session_close_epoch=m0 + 3600,  # market open well past the horizon
    )
    assert status == "complete"
    # baseline: last pre-signal trade (no minute-0 candle)
    assert feats["baseline_price"] == pytest.approx(100.0)
    # as-of fills: T+1 = last trade at T-4; T+3..T+8 = the T+2 trade
    assert feats["price_1m"] == pytest.approx(100.0)
    assert feats["price_3m"] == pytest.approx(101.0)
    assert feats["price_5m"] == pytest.approx(101.0)
    # horizon beyond the last trade but inside the session → filled
    assert feats["price_15m"] == pytest.approx(103.0)
    assert feats["ret_15m_pct"] == pytest.approx(3.0)
    assert feats["label_15m"] == "UP"
    # volume at the signal minute is genuinely unknowable — never as-of
    assert feats["volume_1m"] is None
    assert feats["volume_surge_1m"] is None
    assert feats["range_breakout_2m"] is None

    # WITHOUT a session close the fill stops at the last trade (T+9):
    feats2, status2 = compute_reaction_features(candles, SIGNAL_EPOCH)
    assert status2 == "partial"
    assert feats2["price_15m"] is None


def test_reaction_features_sparse_no_post_trades_is_no_candles():
    m0 = SIGNAL_EPOCH - (SIGNAL_EPOCH % 60)
    only_pre = [[m0 - 300, 100, 100.1, 99.9, 100.0, 10.0]]
    feats, status = compute_reaction_features(
        only_pre, SIGNAL_EPOCH, session_close_epoch=m0 + 3600
    )
    assert status == "no_candles"
    assert feats is None


def test_reaction_features_day_high_beyond_horizon():
    # 25 post-signal minutes: the day keeps running past the 15m horizon.
    candles = make_candles(SIGNAL_EPOCH, post_minutes=25, per_minute_move=0.5)
    feats, status = compute_reaction_features(
        candles, SIGNAL_EPOCH, baseline_price=100.0
    )
    assert status == "complete"
    # day high (k=25 → 100 + 0.5*26 + 0.05) exceeds the horizon high
    assert feats["day_high"] == pytest.approx(113.05)
    assert feats["day_high"] > feats["high_15m"]
    assert feats["ret_day_high_pct"] == pytest.approx(13.05)


@pytest.mark.asyncio
async def test_builder_marks_after_hours_without_api_call(db_session, isolated_db):
    # Last Saturday, 11:30 IST — the market can never have candles there.
    now = utcnow_naive()
    days_back = (now.weekday() - 5) % 7 or 7
    saturday = (now - timedelta(days=days_back)).replace(
        hour=6, minute=0, second=0, microsecond=0
    )
    seed_outcome(db_session, created_at=saturday)
    calls = {"n": 0}

    async def counting_candles(symbol, signal_time):
        calls["n"] += 1
        return []

    builder = DatasetBuilder(candle_fn=counting_candles, market_hours_check=True)
    counts = await builder.run_once()
    assert counts["after_hours"] == 1
    assert calls["n"] == 0  # decided WITHOUT an API call
    row = db_session.query(DatasetFeature).one()
    assert row.status == "after_hours"
    # terminal — never retried
    counts2 = await builder.run_once()
    assert counts2["processed"] == 0


@pytest.mark.asyncio
async def test_builder_caches_candles_per_symbol(db_session, isolated_db):
    # Three same-symbol outcomes in one batch → ONE history fetch.
    outs = [seed_outcome(db_session, minutes_ago=20 + i * 3) for i in range(3)]
    epoch = int(outs[-1].created_at.replace(tzinfo=timezone.utc).timestamp())
    candles = make_candles(epoch, per_minute_move=0.5, pre_minutes=10, post_minutes=30)
    calls = {"n": 0}

    async def counting_candles(symbol, signal_time):
        calls["n"] += 1
        return candles

    builder = DatasetBuilder(candle_fn=counting_candles)
    counts = await builder.run_once()
    assert counts["processed"] == 3
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_run_full_backfills_everything(db_session, isolated_db):
    for i in range(3):
        seed_outcome(db_session, symbol=f"F{i}", minutes_ago=25 + i)

    async def candle_fn(symbol, signal_time):
        epoch = int(signal_time.replace(tzinfo=timezone.utc).timestamp())
        return make_candles(epoch, per_minute_move=0.5)

    builder = DatasetBuilder(candle_fn=candle_fn)
    progress = await builder.run_full(batch=1)  # force multiple batches
    assert progress["running"] is False
    assert progress["batches"] == 3
    assert progress["complete"] == 3
    assert builder.count_pending() == {"signal": 0, "shadow": 0}


@pytest.mark.asyncio
async def test_rebuild_re_enriches_stale_schema_rows(db_session, isolated_db):
    from app.services.dataset_builder import FEATURES_VERSION

    # A complete row from an OLDER schema (no features_version key).
    o = seed_outcome(db_session)
    db_session.add(
        DatasetFeature(
            outcome_id=o.id, symbol=o.symbol, signal_time=o.created_at,
            status="complete", attempts=3,
            features={"price_1m": 101.0, "ret_15m_pct": 4.0, "label_15m": "UP"},
        )
    )
    db_session.commit()

    async def candle_fn(symbol, signal_time):
        epoch = int(signal_time.replace(tzinfo=timezone.utc).timestamp())
        return make_candles(epoch, per_minute_move=0.5)

    builder = DatasetBuilder(candle_fn=candle_fn)
    # A normal run ignores the complete row (it's not retryable)...
    assert (await builder.run_once())["processed"] == 0
    # ...but count_pending / rebuild see it as stale schema.
    builder._include_stale = True
    assert builder.count_pending()["signal"] == 1
    counts = await builder.run_once()
    assert counts["processed"] == 1 and counts["complete"] == 1

    db_session.expire_all()
    row = db_session.query(DatasetFeature).filter(
        DatasetFeature.outcome_id == o.id
    ).one()
    assert row.features["features_version"] == FEATURES_VERSION
    # the new full trajectory is now present
    assert "price_10m" in row.features
    assert row.features["price_10m"] is not None


def seed_shadow_announcement(
    db, *, symbol="WIPRO", event_type="OTHER", minutes_ago=25, recommendation="HOLD"
):
    """announcement + analysis, NO signal — the rejected universe."""
    ann = Announcement(
        symbol=symbol,
        exchange="NSE",
        event_type=event_type,
        headline=f"{symbol} administrative filing",
        filed_at=utcnow_naive() - timedelta(minutes=minutes_ago + 1),
        content_hash=f"shadow-{symbol}-{minutes_ago}-{db.query(Announcement).count()}",
    )
    db.add(ann)
    db.flush()
    analysis = Analysis(
        announcement_id=ann.id,
        model="deepseek-chat",
        sentiment="neutral",
        sentiment_score=5.0,
        confidence=0.4,
        recommendation=recommendation,
        created_at=utcnow_naive() - timedelta(minutes=minutes_ago),
    )
    db.add(analysis)
    db.commit()
    return ann


@pytest.mark.asyncio
async def test_builder_enriches_shadow_announcements(db_session, isolated_db):
    ann = seed_shadow_announcement(db_session)
    analysis_created = (
        db_session.query(Analysis)
        .filter(Analysis.announcement_id == ann.id)
        .one()
        .created_at
    )
    epoch = int(analysis_created.replace(tzinfo=timezone.utc).timestamp())
    candles = make_candles(epoch, per_minute_move=0.1)
    index_candles = make_candles(epoch, per_minute_move=0.5, base=25000.0)

    async def candle_fn(symbol, signal_time):
        return candles

    async def index_fn(signal_time):
        return index_candles

    builder = DatasetBuilder(candle_fn=candle_fn, index_fn=index_fn)
    counts = await builder.run_once()
    assert counts["processed"] == 1
    assert counts["shadow"] == 1
    assert counts["complete"] == 1

    row = db_session.query(DatasetFeature).one()
    assert row.announcement_id == ann.id
    assert row.outcome_id is None
    # context features rode along
    assert row.features["day_of_week"] is not None
    assert row.features["minute_since_open"] is not None
    assert row.features["nifty_ret_5m_pct"] is not None
    assert row.features["excess_ret_5m_pct"] == pytest.approx(
        row.features["ret_5m_pct"] - row.features["nifty_ret_5m_pct"], abs=1e-3
    )

    # idempotent — nothing left on the second pass
    counts2 = await builder.run_once()
    assert counts2["processed"] == 0


@pytest.mark.asyncio
async def test_builder_prioritises_signal_rows_over_shadow(db_session, isolated_db):
    seed_outcome(db_session)
    seed_shadow_announcement(db_session)

    async def candle_fn(symbol, signal_time):
        return []

    builder = DatasetBuilder(candle_fn=candle_fn)
    counts = await builder.run_once(limit=1)  # room for only one
    assert counts["processed"] == 1
    assert counts["shadow"] == 0  # the signal row won the slot


# ---- API --------------------------------------------------------------------


def seed_chain(db, *, symbol="RELIANCE", event_type="ORDER_WIN", enrich=True):
    """announcement → analysis → signal → outcome (+ trade + features)."""
    ann = Announcement(
        symbol=symbol,
        exchange="NSE",
        event_type=event_type,
        headline=f"{symbol} bags order",
        filed_at=utcnow_naive() - timedelta(minutes=31),
        content_hash=f"ds-{symbol}-{event_type}-{db.query(Announcement).count()}",
    )
    db.add(ann)
    db.flush()
    analysis = Analysis(
        announcement_id=ann.id,
        model="deepseek-chat",
        sentiment="positive",
        sentiment_score=80.0,
        confidence=0.9,
        recommendation="BUY",
        raw_response={"timings": {"total_ms": 2500}},
    )
    db.add(analysis)
    db.flush()
    signal = Signal(
        analysis_id=analysis.id,
        symbol=symbol,
        action="BUY",
        confidence=0.9,
        status="filled",
        rationale="auto entry entry=100.00 sl=97.00 target=106.00 rr=2.00",
    )
    db.add(signal)
    db.flush()
    outcome = SignalOutcome(
        signal_id=signal.id,
        symbol=symbol,
        action="BUY",
        signal_status="pending",
        price_at_signal=100.0,
        move_5m_pct=1.2,
        created_at=utcnow_naive() - timedelta(minutes=30),
    )
    db.add(outcome)
    db.flush()
    db.add(
        TradeRow(
            signal_id=signal.id,
            symbol=symbol,
            side="BUY",
            quantity=10,
            price=100.1,
            status="closed",
            pnl=55.0,
            slippage_pct=0.1,
            r_multiple=1.8,
        )
    )
    if enrich:
        db.add(
            DatasetFeature(
                outcome_id=outcome.id,
                signal_id=signal.id,
                symbol=symbol,
                signal_time=outcome.created_at,
                status="complete",
                attempts=3,
                features={
                    "price_1m": 101.0,
                    "ret_1m_pct": 1.0,
                    "ret_15m_pct": 4.0,
                    "high_15m": 105.0,
                    "mfe_15m_pct": 5.0,
                    "label_15m": "UP",
                    "volume_surge_1m": 5.0,
                },
            )
        )
    db.commit()
    return outcome


def test_columns_catalog(client):
    r = client.get("/api/dataset/columns")
    assert r.status_code == 200
    body = r.json()
    keys = {c["key"] for c in body["columns"]}
    # feature/target split is the whole point — spot-check both sides
    roles = {c["key"]: c["role"] for c in body["columns"]}
    assert roles["ret_1m_pct"] == "feature"
    assert roles["ret_15m_pct"] == "target"
    assert roles["label_15m"] == "target"
    assert "model_a_direction" in body["presets"]
    # presets only reference real columns
    for preset in body["presets"].values():
        assert set(preset) <= keys


def test_dataset_rows_join_and_filters(client, db_session, isolated_db):
    seed_chain(db_session, symbol="RELIANCE", event_type="ORDER_WIN")
    seed_chain(db_session, symbol="TCS", event_type="BUYBACK", enrich=False)

    r = client.get("/api/dataset?limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    by_symbol = {row["symbol"]: row for row in body["rows"]}
    rel = by_symbol["RELIANCE"]
    # joined columns from every source
    assert rel["event_type"] == "ORDER_WIN"
    assert rel["sentiment_score"] == 80.0
    assert rel["analysis_latency_ms"] == 2500
    assert rel["planned_entry"] == 100.0
    assert rel["planned_stop"] == 97.0
    assert rel["entry_fill_price"] == 100.1
    assert rel["realized_pnl"] == 55.0
    assert rel["trade_taken"] is True
    assert rel["ret_1m_pct"] == 1.0
    assert rel["label_15m"] == "UP"
    assert rel["enrich_status"] == "complete"
    assert by_symbol["TCS"]["enrich_status"] == "pending"
    assert by_symbol["TCS"]["label_15m"] is None

    # filters
    r = client.get("/api/dataset?event_type=BUYBACK")
    assert [row["symbol"] for row in r.json()["rows"]] == ["TCS"]
    r = client.get("/api/dataset?enriched_only=true")
    assert [row["symbol"] for row in r.json()["rows"]] == ["RELIANCE"]
    r = client.get("/api/dataset?label=UP")
    assert [row["symbol"] for row in r.json()["rows"]] == ["RELIANCE"]
    r = client.get("/api/dataset?taken=taken")
    assert r.json()["total"] == 2  # both signals are status=filled


def test_dataset_column_selection(client, db_session, isolated_db):
    seed_chain(db_session)
    r = client.get("/api/dataset?columns=symbol,ret_1m_pct,label_15m,bogus")
    assert r.status_code == 200
    row = r.json()["rows"][0]
    assert set(row.keys()) == {"symbol", "ret_1m_pct", "label_15m"}


def test_dataset_stats(client, db_session, isolated_db):
    seed_chain(db_session, symbol="RELIANCE")
    seed_chain(db_session, symbol="TCS", event_type="BUYBACK", enrich=False)
    r = client.get("/api/dataset/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_rows"] == 2
    assert body["enrichment"]["complete"] == 1
    assert body["enrichment"]["pending"] == 1
    assert body["label_balance"] == {"UP": 1}
    events = {e["event_type"]: e["samples"] for e in body["by_event_type"]}
    assert events == {"ORDER_WIN": 1, "BUYBACK": 1}


def test_dataset_export_csv_and_jsonl(client, db_session, isolated_db):
    seed_chain(db_session)
    r = client.get("/api/dataset/export?format=csv&columns=symbol,ret_15m_pct,label_15m")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    lines = r.text.strip().splitlines()
    assert lines[0] == "symbol,ret_15m_pct,label_15m"
    assert lines[1] == "RELIANCE,4.0,UP"

    r = client.get("/api/dataset/export?format=jsonl&columns=symbol,label_15m")
    assert r.status_code == 200
    import json as _json

    row = _json.loads(r.text.strip().splitlines()[0])
    assert row == {"symbol": "RELIANCE", "label_15m": "UP"}


def test_dataset_shadow_rows_and_source_filter(client, db_session, isolated_db):
    seed_chain(db_session, symbol="RELIANCE")
    seed_shadow_announcement(db_session, symbol="WIPRO")

    body = client.get("/api/dataset?limit=50").json()
    assert body["total"] == 2
    by_source = {r["row_source"]: r for r in body["rows"]}
    assert by_source["signal"]["symbol"] == "RELIANCE"
    shadow = by_source["shadow"]
    assert shadow["symbol"] == "WIPRO"
    assert shadow["outcome_id"] is None
    assert shadow["announcement_id"] is not None
    assert shadow["action"] == "HOLD"  # the AI recommendation
    assert shadow["trade_taken"] is False

    # source filter
    body = client.get("/api/dataset?source=shadow").json()
    assert [r["symbol"] for r in body["rows"]] == ["WIPRO"]
    # shadow rows were never taken → excluded by taken=taken
    body = client.get("/api/dataset?taken=taken").json()
    assert [r["symbol"] for r in body["rows"]] == ["RELIANCE"]
    # ...and all count as blocked/untaken
    body = client.get("/api/dataset?taken=blocked").json()
    assert [r["symbol"] for r in body["rows"]] == ["WIPRO"]


def test_dataset_dedup_cross_listings(client, db_session, isolated_db):
    # The same news filed on NSE (traded) and BSE (noise-filtered):
    # identical symbol + filed-minute → one cross_listing_key.
    filed = utcnow_naive() - timedelta(minutes=40)
    out = seed_chain(db_session, symbol="RELIANCE")
    db_session.query(Announcement).filter(
        Announcement.symbol == "RELIANCE"
    ).update({"filed_at": filed})
    twin = seed_shadow_announcement(db_session, symbol="RELIANCE")
    db_session.query(Announcement).filter(Announcement.id == twin.id).update(
        {"filed_at": filed, "exchange": "BSE"}
    )
    db_session.commit()

    body = client.get("/api/dataset?limit=50").json()
    assert body["total"] == 2
    keys = {r["cross_listing_key"] for r in body["rows"]}
    assert len(keys) == 1  # twins share the key

    r = client.get(
        "/api/dataset/export?format=jsonl&dedup=true&columns=symbol,row_source"
    )
    import json as _json

    lines = [_json.loads(l) for l in r.text.strip().splitlines()]
    assert len(lines) == 1
    assert lines[0]["row_source"] == "signal"  # signal row beats its shadow twin
    assert out.symbol == "RELIANCE"


def test_dataset_chronological_split(client, db_session, isolated_db):
    # 5 signals over 5 days, oldest first = idx 0.
    for i in range(5):
        o = seed_chain(db_session, symbol=f"S{i}", enrich=False)
        db_session.query(SignalOutcome).filter(SignalOutcome.id == o.id).update(
            {"created_at": utcnow_naive() - timedelta(days=5 - i)}
        )
    db_session.commit()

    def _symbols(split):
        r = client.get(
            f"/api/dataset/export?format=jsonl&split={split}&split_ratio=0.8&columns=symbol"
        )
        import json as _json

        return [_json.loads(l)["symbol"] for l in r.text.strip().splitlines()]

    train, val = _symbols("train"), _symbols("val")
    assert train == ["S0", "S1", "S2", "S3"]  # oldest 80%, chronological
    assert val == ["S4"]  # the newest slice validates
    assert not (set(train) & set(val))


def test_dataset_health_flags_leakage(client, db_session, isolated_db):
    # 35 rows where a "feature" exactly tracks the target → leak flag.
    for i in range(35):
        o = seed_outcome(db_session, symbol=f"H{i}", minutes_ago=30 + i)
        db_session.add(
            DatasetFeature(
                outcome_id=o.id,
                symbol=o.symbol,
                signal_time=o.created_at,
                status="complete",
                attempts=3,
                features={
                    "ret_1m_pct": float(i),      # perfectly correlated → leak
                    "volume_surge_1m": 5.0,      # constant → no correlation
                    "ret_15m_pct": float(i),     # the target
                    "label_15m": "UP",
                },
            )
        )
    db_session.commit()

    body = client.get("/api/dataset/health?target=ret_15m_pct").json()
    assert body["rows_sampled"] == 35
    cols = {c["key"]: c for c in body["columns"]}
    assert cols["ret_1m_pct"]["corr_target"] == pytest.approx(1.0)
    assert cols["ret_1m_pct"]["leak_warning"] is True
    assert cols["volume_surge_1m"]["leak_warning"] is False
    assert cols["ret_15m_pct"]["null_rate"] == 0.0
    assert cols["price_1m"]["null_rate"] == 1.0  # never populated here
    # the target itself is never flagged
    assert "leak_warning" not in cols["ret_15m_pct"] or not cols["ret_15m_pct"]["leak_warning"]


def test_backfill_endpoint(client, db_session, isolated_db, monkeypatch):
    outcome = seed_outcome(db_session)
    epoch = int(outcome.created_at.replace(tzinfo=timezone.utc).timestamp())
    candles = make_candles(epoch, per_minute_move=0.5)

    async def stub_candles(symbol, signal_time):
        return candles

    from app.services import dataset_builder as db_mod

    monkeypatch.setattr(db_mod, "_default_candle_fn", stub_candles)
    r = client.post("/api/dataset/backfill?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["complete"] == 1
    row = db_session.query(DatasetFeature).one()
    assert row.status == "complete"


def test_backfill_retry_failed_resets_attempts(client, db_session, isolated_db):
    # Stranded rows (attempts exhausted) + one complete row.
    for i, status in enumerate(["no_candles", "no_candles", "partial", "complete"]):
        o = seed_outcome(db_session, symbol=f"R{i}", minutes_ago=30 + i)
        db_session.add(
            DatasetFeature(
                outcome_id=o.id, symbol=o.symbol, signal_time=o.created_at,
                status=status, attempts=3,
                features={"ret_15m_pct": 1.0} if status == "complete" else None,
            )
        )
    db_session.commit()

    r = client.post("/api/dataset/backfill?limit=1&retry_failed=true")
    assert r.status_code == 200
    assert r.json()["retried"] == 3  # the two no_candles + the partial

    rows = {r_.symbol: r_ for r_ in db_session.query(DatasetFeature).all()}
    for row in rows.values():
        db_session.refresh(row)
    assert rows["R3"].attempts == 3  # complete row untouched
    # at least the batch-processed row was retried; none of the failed
    # rows still carries exhausted attempts EXCEPT any the batch just
    # re-attempted (attempts restart from 0 → 1 after a re-run)
    assert all(r_.attempts <= 1 for k, r_ in rows.items() if k != "R3")


def test_full_backfill_endpoint_and_status(client, db_session, isolated_db, monkeypatch):
    import time

    outcome = seed_outcome(db_session)
    epoch = int(outcome.created_at.replace(tzinfo=timezone.utc).timestamp())
    candles = make_candles(epoch, per_minute_move=0.5)

    async def stub_candles(symbol, signal_time):
        return candles

    from app.services import dataset_builder as db_mod

    monkeypatch.setattr(db_mod, "_default_candle_fn", stub_candles)

    r = client.post("/api/dataset/backfill?full=true")
    assert r.status_code == 200
    assert r.json()["full"] is True

    # Poll status until the background task drains the queue. Generous
    # deadline: under full-suite load the shared executor threads are
    # contended, so a tight timeout flakes.
    deadline = time.monotonic() + 30.0
    status = {}
    while time.monotonic() < deadline:
        status = client.get("/api/dataset/backfill/status").json()
        if not status["progress"]["running"] and status["progress"].get("finished_at"):
            break
        time.sleep(0.1)
    assert status["progress"]["running"] is False
    assert status["progress"]["complete"] == 1
    assert status["remaining"] == {"signal": 0, "shadow": 0}
    row = db_session.query(DatasetFeature).one()
    assert row.status == "complete"
