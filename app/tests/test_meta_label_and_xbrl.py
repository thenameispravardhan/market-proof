"""Meta-labeling (secondary model on replayed trades) and the results-XBRL
numeric-surprise parser."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from app.research import meta_label as ml
from app.research import results_xbrl as rx
from app.research.replay import ReplayTrade


# ---------------------------------------------------------------------------
# Meta-labeling
# ---------------------------------------------------------------------------


def _trade(i: int, *, win: bool, pre: float, conf: float, event="ORDER_WIN") -> ReplayTrade:
    t = datetime(2026, 1, 5, 10, 0) + timedelta(hours=6 * i)
    r = 1.5 if win else -1.0
    return ReplayTrade(
        signal_id=i, symbol="S", side=1, event_type=event, confidence=conf, status="blocked",
        block_reason="RULE_HOLD", direction_source="signal", entry_at=t.isoformat(), entry=100.0,
        stop=98.0, target=105.0, exit_at=(t + timedelta(minutes=10)).isoformat(), exit=101.0,
        exit_reason="TARGET" if win else "STOP_LOSS", qty=1000, gross_pnl=r * 2000, charges=80.0,
        net_pnl=r * 2000 - 80, r_multiple=r, pre_move_pct=pre, post_move_pct=r, day=t.date().isoformat())


def _synthetic(n=600, seed=0):
    """Winners are the trades where little had moved before entry, and the
    LLM's confidence is anti-predictive: the setting the README describes."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        pre = rng.uniform(-1, 4)
        win = rng.random() < (0.75 if pre < 1.0 else 0.2)
        conf = rng.uniform(0.7, 1.0) if not win else rng.uniform(0.2, 0.8)
        out.append(_trade(i, win=win, pre=pre, conf=conf))
    return out


def test_features_never_include_the_outcome():
    f = ml.trade_features(_trade(0, win=True, pre=0.5, conf=0.8))
    assert not any(k.startswith(ml.LEAK_PREFIXES) for k in f)
    with pytest.raises(AssertionError):
        ml.Design.fit([{"net_pnl": 1.0}])


def test_isotonic_is_monotone_and_fits_steps():
    iso = ml.Isotonic().fit([0.1, 0.2, 0.3, 0.4, 0.5, 0.6], [0, 0, 1, 0, 1, 1])
    pred = iso.predict([0.0, 0.15, 0.35, 0.55, 0.9])
    assert pred == sorted(pred) and pred[0] == 0.0 and pred[-1] == 1.0


def test_kelly_fraction():
    assert ml.kelly_fraction(0.5, 2.0) == pytest.approx(0.25)
    assert ml.kelly_fraction(0.3, 1.0) == 0.0          # no edge -> no bet, never negative
    assert ml.kelly_fraction(0.6, 0.0) == 0.0


def test_meta_model_beats_llm_confidence_and_keeps_better_trades():
    res = ml.run(_synthetic())
    assert res["ok"]
    assert res["split"] == {"fit": 360, "calibrate": 120, "test": 120, "test_from": res["split"]["test_from"]}
    assert res["test_auc_meta"] > 0.75 > res["test_auc_llm_confidence"]
    assert res["auc_gain_over_confidence"]["ci_excludes_zero"]
    kept, allt = res["kept_by_meta_model"], res["all_test_trades"]
    assert kept["n"] < allt["n"] and kept["expectancy_r"] > allt["expectancy_r"]
    assert res["ece_calibrated"] <= res["ece_raw"] + 0.05
    assert res["top_weights"][0]["feature"] in ("pre_move_pct", "confidence")
    mults = [r["risk_multiplier_vs_base"] for r in res["kelly"]["by_probability"]]
    assert all(m is None or 0 <= m <= 1.5 for m in mults)


def test_meta_model_refuses_too_little_data():
    assert ml.run(_synthetic(50))["ok"] is False


# ---------------------------------------------------------------------------
# Results XBRL
# ---------------------------------------------------------------------------


def _xbrl(quarters: dict[str, dict[str, float]], segment_revenue: float = 0.0) -> bytes:
    """A minimal Ind-AS results instance: one context per quarter, plus a
    dimensional (segment) context that must be ignored."""
    ctx, facts = [], []
    for i, (end, vals) in enumerate(quarters.items()):
        end_d = datetime.fromisoformat(end).date()
        start_d = end_d - timedelta(days=90)
        ctx.append(f'<xbrli:context id="C{i}"><xbrli:entity><xbrli:identifier scheme="x">X</xbrli:identifier>'
                   f'</xbrli:entity><xbrli:period><xbrli:startDate>{start_d}</xbrli:startDate>'
                   f'<xbrli:endDate>{end_d}</xbrli:endDate></xbrli:period></xbrli:context>')
        for k, v in vals.items():
            facts.append(f'<in-bse-fin:{k} contextRef="C{i}" unitRef="INR" decimals="-5">{v}</in-bse-fin:{k}>')
    ctx.append('<xbrli:context id="SEG"><xbrli:entity><xbrli:identifier scheme="x">X</xbrli:identifier>'
               '<xbrli:segment><xbrldi:explicitMember dimension="a:b">a:Seg1</xbrldi:explicitMember>'
               '</xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate>'
               '<xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>')
    facts.append(f'<in-bse-fin:RevenueFromOperations contextRef="SEG" unitRef="INR">{segment_revenue}</in-bse-fin:RevenueFromOperations>')
    return ('<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
            'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin">'
            + "".join(ctx) + "".join(facts) + "</xbrli:xbrl>").encode()


def test_yoy_qoq_and_rebuilt_ebitda():
    doc = _xbrl({
        "2026-06-30": {"RevenueFromOperations": 1200, "ProfitBeforeTax": 150, "FinanceCosts": 20,
                       "DepreciationDepletionAndAmortisationExpense": 30, "ProfitLossForPeriod": 110},
        "2026-03-31": {"RevenueFromOperations": 1000, "ProfitBeforeTax": 100, "FinanceCosts": 20,
                       "DepreciationDepletionAndAmortisationExpense": 30, "ProfitLossForPeriod": 80},
        "2025-06-30": {"RevenueFromOperations": 800, "ProfitBeforeTax": 50, "FinanceCosts": 10,
                       "DepreciationDepletionAndAmortisationExpense": 20, "ProfitLossForPeriod": -10},
    }, segment_revenue=999999)
    f = rx.surprise_features(rx.parse_instance(doc))
    assert f["ok"] and f["quarter_end"] == "2026-06-30"
    assert f["revenue"] == 1200                      # the segment context was ignored
    assert f["revenue_yoy_pct"] == 50.0 and f["revenue_qoq_pct"] == 20.0
    assert f["ebitda"] == 200 and f["ebitda_yoy_pct"] == pytest.approx(150.0)
    assert f["pat_yoy_pct"] == pytest.approx(1200.0)  # vs a loss: measured against |prev|
    assert f["loss_to_profit"] is True
    assert f["pat_margin_change_pp_yoy"] == pytest.approx(110 / 1200 * 100 - (-10 / 800 * 100), abs=0.01)


def test_missing_history_gives_none_not_zero():
    f = rx.surprise_features(rx.parse_instance(_xbrl({"2026-06-30": {"RevenueFromOperations": 100}})))
    assert f["revenue_yoy_pct"] is None and f["ebitda"] is None
    assert rx.surprise_features({})["ok"] is False


def test_url_allowlist():
    assert rx.allowed_url("https://nsearchives.nseindia.com/corporate/xbrl/X.xml")
    assert rx.allowed_url("https://www.bseindia.com/xml-data/x.xml")
    assert not rx.allowed_url("http://nsearchives.nseindia.com/x.xml")
    assert not rx.allowed_url("https://evil-nseindia.com/x.xml")
    assert not rx.allowed_url("https://169.254.169.254/latest/meta-data")


def test_results_endpoint_refuses_foreign_urls(client):
    body = client.get("/api/research/results-xbrl", params={"url": "https://example.com/a.xml"}).json()
    assert body["ok"] is False and "nseindia" in body["reason"]
