"""Tests for the HOLD-calibration analysis (pure — no DB).

Covers the excursion-magnitude math, dataset-row normalisation, bucket
aggregation, and the three verdict scenarios (too tight / discriminating
/ insufficient) the report is supposed to distinguish.
"""
from __future__ import annotations

from app.services.hold_calibration import (
    compute_calibration,
    excursion_magnitude,
    normalise_dataset_row,
    summarise_bucket,
)


# ---- excursion magnitude ------------------------------------------------


def test_excursion_prefers_larger_of_mfe_mae():
    # Fell 3% (adverse) while only rising 1% (favorable) → the move was 3%.
    assert excursion_magnitude(1.0, -3.0, 0.2) == 3.0
    assert excursion_magnitude(4.0, -0.5, 0.2) == 4.0


def test_excursion_falls_back_to_net_return():
    assert excursion_magnitude(None, None, -2.0) == 2.0


def test_excursion_none_when_nothing_known():
    assert excursion_magnitude(None, None, None) is None


# ---- row normalisation --------------------------------------------------


def test_normalise_uppercases_recommendation_and_computes_excursion():
    row = {
        "symbol": "sbin", "recommendation": "hold", "trade_taken": False,
        "event_type": "ORDER_WIN", "confidence": 0.4, "sentiment": "positive",
        "mfe_15m_pct": 0.5, "mae_15m_pct": -2.8, "ret_15m_pct": -2.1,
        "label_15m": "DOWN",
    }
    out = normalise_dataset_row(row)
    assert out["recommendation"] == "HOLD"
    assert out["taken"] is False
    assert out["abs_excursion"] == 2.8
    assert out["label_15m"] == "DOWN"


def test_normalise_missing_event_type_becomes_unknown():
    out = normalise_dataset_row({"recommendation": "HOLD"})
    assert out["event_type"] == "UNKNOWN"
    assert out["abs_excursion"] is None  # no labels → incomplete


# ---- bucket aggregation -------------------------------------------------


def _sample(exc, ret=0.0, label="FLAT"):
    return {"abs_excursion": exc, "ret_15m_pct": ret, "label_15m": label}


def test_summarise_counts_movers_and_big_movers():
    samples = [
        _sample(0.5, 0.1, "FLAT"),
        _sample(2.0, 1.9, "UP"),
        _sample(3.5, -3.4, "DOWN"),
        _sample(4.0, 3.9, "UP"),
    ]
    b = summarise_bucket(samples, move_threshold_pct=1.5, big_threshold_pct=3.0)
    assert b["n"] == 4
    assert b["movers"] == 3            # 2.0, 3.5, 4.0 >= 1.5
    assert b["mover_rate"] == 0.75
    assert b["big_movers"] == 2        # 3.5, 4.0 >= 3.0
    assert b["up"] == 2 and b["down"] == 1 and b["flat"] == 1
    assert b["median_abs_excursion"] == 2.75


def test_summarise_empty_is_zeroed_not_error():
    b = summarise_bucket([], move_threshold_pct=1.5, big_threshold_pct=3.0)
    assert b["n"] == 0 and b["mover_rate"] == 0.0
    assert b["avg_abs_excursion"] is None


# ---- full report --------------------------------------------------------


def _hold(exc, event="OTHER", conf=0.5, taken=False, ret=None, label="FLAT", sym="X"):
    return {
        "symbol": sym, "recommendation": "HOLD", "taken": taken,
        "event_type": event, "confidence": conf, "sentiment": "neutral",
        "abs_excursion": exc, "ret_15m_pct": (ret if ret is not None else exc),
        "label_15m": label,
    }


def test_report_structure_and_hold_slice():
    rows = (
        [_hold(2.0, event="ORDER_WIN") for _ in range(10)]
        + [_hold(0.4, event="BOARD_MEETING") for _ in range(10)]
        # a taken BUY that moved — appears in comparison, not in HOLD slice
        + [{
            "symbol": "T", "recommendation": "BUY", "taken": True,
            "event_type": "ORDER_WIN", "confidence": 0.8, "sentiment": "positive",
            "abs_excursion": 3.0, "ret_15m_pct": 3.0, "label_15m": "UP",
        }]
        # an incomplete row (no label) must be excluded everywhere
        + [_hold(None, event="OTHER")]
    )
    rep = compute_calibration(rows, move_threshold_pct=1.5, big_threshold_pct=3.0)
    assert rep["sample"]["total_complete"] == 21  # the None row dropped
    assert rep["sample"]["hold"] == 20
    assert rep["sample"]["taken"] == 1
    assert rep["hold_overall"]["movers"] == 10    # the ten 2.0% HOLDs
    # by_event_type sorted by mover_rate desc → ORDER_WIN (1.0) first.
    assert rep["by_event_type"][0]["key"] == "ORDER_WIN"
    assert rep["by_event_type"][0]["mover_rate"] == 1.0
    # top_missed surfaces the biggest HOLD movers first.
    assert rep["top_missed"][0]["abs_excursion"] == 2.0


def test_verdict_flags_rules_too_tight():
    # Declined filings move as often as taken ones → selection adds nothing.
    declined = [_hold(2.0, taken=False) for _ in range(40)]  # 100% movers
    taken = [
        {"symbol": "t", "recommendation": "BUY", "taken": True,
         "event_type": "X", "confidence": 0.8, "sentiment": "positive",
         "abs_excursion": (2.0 if i < 20 else 0.4), "ret_15m_pct": 1.0,
         "label_15m": "UP"}
        for i in range(40)
    ]  # 50% movers
    rep = compute_calibration(declined + taken, move_threshold_pct=1.5)
    assert "too tight" in rep["verdict"]


def test_verdict_reports_discriminating():
    declined = [_hold((2.0 if i < 4 else 0.3), taken=False) for i in range(40)]  # 10%
    taken = [
        {"symbol": "t", "recommendation": "BUY", "taken": True,
         "event_type": "X", "confidence": 0.8, "sentiment": "positive",
         "abs_excursion": 2.0, "ret_15m_pct": 2.0, "label_15m": "UP"}
        for _ in range(20)
    ]  # 100%
    rep = compute_calibration(declined + taken, move_threshold_pct=1.5)
    assert "discriminating" in rep["verdict"]


def test_verdict_insufficient_when_few_declined():
    rep = compute_calibration(
        [_hold(2.0, taken=False) for _ in range(5)], move_threshold_pct=1.5
    )
    assert "Not enough" in rep["verdict"]


# ---- min_bucket_n noise floor -------------------------------------------


def test_min_bucket_n_hides_thin_buckets_and_reports_how_many():
    """A bucket of n=1 that "moved 100% of the time" is noise, not a finding."""
    rows = (
        [_hold(2.0, event="ORDER_WIN") for _ in range(10)]
        + [_hold(2.0, event="ONE_OFF")]          # n=1, 100% mover rate
        + [_hold(2.0, event="TWO_OFF") for _ in range(2)]
    )
    loose = compute_calibration(rows, min_bucket_n=1)
    assert {b["key"] for b in loose["by_event_type"]} == {
        "ORDER_WIN", "ONE_OFF", "TWO_OFF"
    }
    assert loose["dropped_buckets"] == 0

    tight = compute_calibration(rows, min_bucket_n=3)
    assert [b["key"] for b in tight["by_event_type"]] == ["ORDER_WIN"]
    # Silently truncating would read as "there was nothing else".
    assert tight["dropped_buckets"] == 2


def test_top_n_bounds_the_missed_list():
    rows = [_hold(float(i), event="OTHER") for i in range(1, 30)]
    assert len(compute_calibration(rows, top_n=5)["top_missed"]) == 5
    # Biggest first, so a shorter list never drops the worst miss.
    assert compute_calibration(rows, top_n=5)["top_missed"][0]["abs_excursion"] == 29.0


# ---- confidence verdict --------------------------------------------------


def _conf(exc, confidence):
    return {
        "symbol": "X", "recommendation": "HOLD", "taken": False,
        "event_type": "OTHER", "confidence": confidence, "sentiment": "neutral",
        "abs_excursion": exc, "ret_15m_pct": exc, "label_15m": "UP",
    }


def test_confidence_verdict_detects_inversion():
    """The live 2026-08-26 shape: most-confident band moves LEAST."""
    rows = (
        [_conf(2.0, 0.1) for _ in range(40)]     # low conf, 100% movers
        + [_conf(0.2, 0.95) for _ in range(40)]  # high conf, 0% movers
    )
    v = compute_calibration(rows, move_threshold_pct=1.5)["confidence_verdict"]
    assert v is not None and "INVERTED" in v


def test_confidence_verdict_detects_tracking():
    rows = (
        [_conf(0.2, 0.1) for _ in range(40)]
        + [_conf(2.0, 0.95) for _ in range(40)]
    )
    v = compute_calibration(rows, move_threshold_pct=1.5)["confidence_verdict"]
    assert v is not None and "tracks reality" in v


def test_confidence_verdict_none_when_bands_too_thin():
    # Under 30 samples a band cannot support a claim either way.
    rows = [_conf(2.0, 0.1) for _ in range(5)] + [_conf(0.2, 0.95) for _ in range(5)]
    assert compute_calibration(rows)["confidence_verdict"] is None
