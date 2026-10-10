"""Measurement layer: statistics, the cost-aware replay, and the research
endpoints. Every simulated price path below is constructed so the expected
fill, exit and cost can be checked by hand."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from app.algo.engine import charges
from app.research import replay as rp
from app.research import stats
from app.tests.factories import seed_signal_chain


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_bootstrap_ci_brackets_the_mean_and_is_reproducible():
    rng = random.Random(1)
    xs = [rng.gauss(0.5, 1.0) for _ in range(400)]
    lo, hi = stats.bootstrap_ci(xs)
    assert lo < stats.mean(xs) < hi
    assert (lo, hi) == stats.bootstrap_ci(xs)          # seeded
    assert stats.bootstrap_ci([1.0]) == (None, None)


def test_max_drawdown_and_profit_factor():
    assert stats.max_drawdown([10, -5, -10, 20, -3]) == 15
    assert stats.profit_factor([10, -5, 5]) == 3.0
    assert stats.profit_factor([1, 2]) is None


def test_summary_flags_a_ci_that_excludes_zero():
    s = stats.summarize_r([1.0, 1.2, 0.9, 1.1] * 10, [100.0] * 40)
    assert s["ci_excludes_zero"] and s["win_rate"] == 1.0
    noise = stats.summarize_r([1, -1] * 20, [0.0] * 40)
    assert not noise["ci_excludes_zero"]


def test_roc_auc_perfect_inverted_and_ties():
    assert stats.roc_auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert stats.roc_auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    assert stats.roc_auc([0.5] * 4, [0, 1, 0, 1]) == 0.5
    assert stats.roc_auc([0.1, 0.2], [1, 1]) is None


def test_ece_is_zero_for_calibrated_and_large_for_overconfident():
    calibrated = stats.ece([0.25] * 4 + [0.75] * 4, [1, 0, 0, 0, 1, 1, 1, 0], n_bins=4)
    assert calibrated["ece"] == 0.0
    over = stats.ece([0.95] * 10, [0] * 9 + [1], n_bins=10)
    assert over["ece"] == pytest.approx(0.85)


def test_deflated_sharpe_shrinks_with_more_trials():
    rng = random.Random(3)
    rets = [rng.gauss(0.001, 0.01) for _ in range(250)]
    few = stats.deflated_sharpe(rets, n_trials=1)
    many = stats.deflated_sharpe(rets, n_trials=500)
    assert many["benchmark_sharpe"] > few["benchmark_sharpe"] == 0.0
    assert many["dsr"] < few["dsr"]


def test_pbo_is_high_for_pure_noise_and_low_for_a_real_edge():
    rng = random.Random(11)
    noise = [[rng.gauss(0, 1) for _ in range(10)] for _ in range(400)]
    assert stats.pbo_cscv(noise, n_splits=8)["pbo"] > 0.25
    edge = [[rng.gauss(0.3 if j == 0 else 0.0, 1) for j in range(10)] for _ in range(400)]
    assert stats.pbo_cscv(edge, n_splits=8)["pbo"] < 0.1


# ---------------------------------------------------------------------------
# Replay simulation
# ---------------------------------------------------------------------------

DAY = datetime(2026, 9, 14)   # a Monday


def _bars(path: list[tuple[float, float, float, float]], start=(10, 0)) -> list[rp.Bar]:
    t0 = DAY.replace(hour=start[0], minute=start[1])
    return [(t0 + timedelta(minutes=i), o, h, l, c) for i, (o, h, l, c) in enumerate(path)]


def _flat(n, px=100.0):
    return [(px, px, px, px)] * n


def _event(side=1, at=(10, 0, 30), event_type="BOARD_MEETING", **kw):
    ist = DAY.replace(hour=at[0], minute=at[1], second=at[2])
    utc = ist - timedelta(hours=5, minutes=30)
    base = dict(signal_id=1, symbol="ACME", side=side, signal_at=utc, filed_at=utc - timedelta(seconds=30),
                event_type=event_type, confidence=0.8, status="blocked", block_reason="RULE_HOLD",
                direction_source="signal")
    base.update(kw)
    return rp.ReplayEvent(**base)


def _params(**kw):
    p = rp.ReplayParams(entry_delay_s=20, slippage_bps=0, notional=100_000, atr_enabled=False,
                        breakeven_enabled=False, sentiment_decay=False)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_entry_is_the_next_full_minute_open_and_target_pays_rr():
    # Signal 10:00:30 + 20s -> entry at the 10:01 open (100). BOARD_MEETING
    # uses the % stop; the target sits rr x the stop distance away.
    from app.config import get_settings
    from app.risk import event_profiles

    prof = event_profiles.profile_for("BOARD_MEETING").resolved(get_settings())
    dist = 100.0 * prof.sl_default_pct / 100.0
    tgt = 100.0 + dist * prof.target_rr
    bars = _bars(_flat(1, 99.0) + _flat(2) + [(100.0, tgt + 1, 100.0, tgt)] + _flat(5, tgt))
    t, why = rp.simulate(_event(), bars, _params())
    assert why is None
    assert t.entry == 100.0 and t.entry_at.endswith("10:01:00")
    assert t.exit_reason == "TARGET" and t.exit == pytest.approx(tgt)
    qty = 1000
    assert t.qty == qty
    assert t.charges == charges(100.0 * qty, tgt * qty)
    assert t.r_multiple == pytest.approx((t.gross_pnl - t.charges) / (dist * qty), abs=1e-3)


def test_stop_wins_when_one_bar_spans_both_levels():
    bars = _bars(_flat(2) + [(100.0, 150.0, 50.0, 100.0)] + _flat(3))
    t, _ = rp.simulate(_event(), bars, _params())
    assert t.exit_reason == "STOP_LOSS" and t.net_pnl < 0


def test_gap_through_the_stop_fills_at_the_open():
    bars = _bars(_flat(2) + [(80.0, 81.0, 79.0, 80.0)] + _flat(3, 80.0))
    t, _ = rp.simulate(_event(), bars, _params())
    assert t.exit_reason == "STOP_LOSS" and t.exit == 80.0


def test_short_side_mirrors():
    bars = _bars(_flat(2) + [(100.0, 100.0, 50.0, 50.0)] + _flat(3, 50.0))
    t, _ = rp.simulate(_event(side=-1), bars, _params())
    assert t.exit_reason == "TARGET" and t.gross_pnl > 0 and t.post_move_pct > 0


def test_time_exit_and_entry_window():
    from app.config import get_settings
    from app.risk import event_profiles

    hold = event_profiles.profile_for("BOARD_MEETING").resolved(get_settings()).max_hold_seconds
    bars = _bars(_flat(hold // 60 + 10, 100.5))
    t, _ = rp.simulate(_event(), bars, _params())
    assert t.exit_reason == "TIME_EXIT"
    late, why = rp.simulate(_event(at=(15, 5, 0)), _bars(_flat(30), start=(15, 0)), _params())
    assert late is None and why == "outside_entry_window"


def test_slippage_and_costs_make_a_flat_trade_lose():
    bars = _bars(_flat(200))
    t, _ = rp.simulate(_event(), bars, _params(slippage_bps=5))
    assert t.entry == pytest.approx(100.05) and t.net_pnl < 0 and t.charges > 0


def test_pre_move_measures_what_was_gone_before_entry():
    # Filing at 10:00 (price 100), entry at 10:01 open 103: 3% already realised.
    bars = _bars([(100, 100, 100, 100), (103, 103, 103, 103)] + _flat(5, 103.0))
    t, _ = rp.simulate(_event(), bars, _params())
    assert t.pre_move_pct == pytest.approx(3.0)


def test_breakeven_lock_turns_a_loser_into_a_scratch():
    bars = _bars(_flat(2) + [(100.0, 103.0, 100.0, 102.5), (102.0, 102.0, 95.0, 95.0)] + _flat(3, 95.0))
    t, _ = rp.simulate(_event(), bars, _params(breakeven_enabled=True))
    assert t.exit_reason == "BREAKEVEN_STOP" and t.exit == pytest.approx(100.2)


def test_block_reason_parsing():
    assert rp.block_reason("blocked", "x | blocked: NEAR_CIRCUIT (ACME is 1% ...)") == "NEAR_CIRCUIT"
    assert rp.block_reason("blocked", "r | denied: rule.action == HOLD") == "RULE_HOLD"
    assert rp.block_reason("blocked", "r | denied: MAX_SIGNALS_PER_DAY=20 exceeded") == "MAX_SIGNALS_PER_DAY"
    assert rp.block_reason("blocked", "r | denied: pipeline_deadline 31s") == "PIPELINE_DEADLINE"
    assert rp.block_reason("filled", "anything") is None


def test_side_of_prefers_signal_then_recommendation_then_sentiment():
    assert rp.side_of("SELL", "buy", "positive") == (-1, "signal")
    assert rp.side_of("HOLD", "buy", "negative") == (1, "recommendation")
    assert rp.side_of("HOLD", "hold", "negative") == (-1, "sentiment")
    assert rp.side_of("HOLD", "hold", "neutral") == (0, "none")


class _Source:
    def __init__(self, data):
        self.data = data

    def bars(self, symbol, start, end):
        return [b for b in self.data.get(symbol, []) if start <= b[0] < end]


def test_run_replay_and_report_split_taken_from_blocked():
    win = _bars(_flat(2) + [(100.0, 150.0, 100.0, 150.0)] + _flat(3, 150.0))
    lose = _bars(_flat(2) + [(100.0, 100.0, 50.0, 50.0)] + _flat(3, 50.0))
    events = [_event(symbol="WIN", status="filled", block_reason=None, signal_id=1),
              _event(symbol="LOSE", status="blocked", block_reason="HIGH_VIX", signal_id=2),
              _event(symbol="NODATA", signal_id=3)]
    res = rp.run_replay(events, _Source({"WIN": win, "LOSE": lose}), _params())
    assert res.skipped == {"no_candles": 1}
    rep = rp.report(res)
    assert rep["taken"]["n"] == 1 and rep["taken"]["net_pnl"] > 0
    assert rep["by_block_reason"]["HIGH_VIX"]["n"] == 1
    assert rep["by_block_reason"]["HIGH_VIX"]["net_pnl"] < 0
    assert rep["breakeven_pct_at_notional"] == pytest.approx(0.0824, abs=0.001)   # ~₹82 per ₹1 lakh


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_funnel_decomposes_the_block_rate(client, db_session, isolated_db):
    seed_signal_chain(db_session, status="filled", rationale="ok")
    seed_signal_chain(db_session, status="blocked", rationale="r | denied: rule.action == HOLD", action="HOLD")
    seed_signal_chain(db_session, status="blocked", rationale="r | denied: rule.action == HOLD", action="HOLD")
    seed_signal_chain(db_session, status="blocked", rationale="r | blocked: HIGH_VIX (vix 31)")
    body = client.get("/api/research/funnel", params={"days": 7}).json()
    assert body["signals"] == 4 and body["block_rate"] == 0.75
    assert body["by_block_reason"][0] == {"reason": "RULE_HOLD", "n": 2, "share_of_blocked": pytest.approx(0.6667, abs=1e-3)}


def test_calibration_reports_inverted_confidence(client, db_session, isolated_db):
    # Confident filings never move, unsure ones always do: AUC must be 0.
    for i in range(30):
        seed_signal_chain(db_session, symbol=f"H{i}", confidence=0.95, move_30m=0.1, move_5m=0.0)
        seed_signal_chain(db_session, symbol=f"L{i}", confidence=0.2, move_30m=3.0, move_5m=1.0)
    body = client.get("/api/research/calibration", params={"days": 7}).json()
    assert body["overall"]["n"] == 60
    assert body["overall"]["auc"] == 0.0
    assert body["overall"]["base_rate"] == 0.5
    assert "deepseek-chat" in body["by_model"]


def test_replay_endpoint_runs_without_candles(client, db_session, isolated_db, monkeypatch):
    seed_signal_chain(db_session, status="filled")
    monkeypatch.setattr(rp.ParquetCandleSource, "bars", lambda self, s, a, b: [])
    body = client.get("/api/research/replay", params={"days": 7}).json()
    assert body["events_loaded"] == 1 and body["all_directional"]["n"] == 0
    assert body["skipped"] in ({"no_candles": 1}, {"outside_entry_window": 1})


def test_parquet_candle_source_reads_the_bot_candle_store(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    import datetime as dt

    from app.services import candle_sync, warehouse_prices, warehouse_store

    warehouse_store.shutdown()
    monkeypatch.setattr(warehouse_store, "STORE", tmp_path / "wh.duckdb")
    monkeypatch.setattr(warehouse_prices, "CANDLES", tmp_path / "candles")
    monkeypatch.setattr(candle_sync, "CANDLES", tmp_path / "candles")
    (tmp_path / "candles").mkdir()
    base = dt.datetime(2026, 9, 14, 9, 15)
    raw = []
    for i in range(30):
        ts = int((base + dt.timedelta(minutes=i) - dt.timedelta(hours=5, minutes=30))
                 .replace(tzinfo=dt.timezone.utc).timestamp())
        raw.append([ts, 100 + i, 101 + i, 99 + i, 100.5 + i, 1000])
    candle_sync._append("ACME", candle_sync._candle_rows(raw))
    try:
        bars = rp.ParquetCandleSource().bars("ACME", base + dt.timedelta(minutes=5), base + dt.timedelta(minutes=10))
    finally:
        warehouse_store.shutdown()
    assert [b[0].minute for b in bars] == [20, 21, 22, 23, 24]
    assert bars[0][1:] == (105.0, 106.0, 104.0, 105.5)
