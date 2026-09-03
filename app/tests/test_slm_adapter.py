"""The SLM speaks its own schema; this is the mapping onto the bot's.

Inputs below are verbatim shapes from `out/sft_val.jsonl` training targets.
"""
from __future__ import annotations

import json

import pytest

from app.analyzer.schemas import AnalysisResponse
from app.analyzer.slm_adapter import SYSTEM_PROMPT, build_prompt, to_analysis


def _reply(**kw) -> str:
    base = {
        "event_type": "ORDER_WIN", "materiality": "HIGH", "surprise": "MEDIUM",
        "facts": {"amount_inr_cr": 412.0, "amount_basis": "NEW_ORDER",
                  "amount_to_mcap": 0.041},
        "direction": "UP", "mover": True, "shape": "IMMEDIATE",
        "price_path": [3, 8, 14, 17, 19, 21, 22, 22, 23, 24, 24, 25, 25, 26, 26, 31, 28],
        "volume_path": [9.1] * 17,
    }
    base.update(kw)
    return json.dumps(base)


def test_mapped_output_satisfies_the_live_schema():
    mapped, raw = to_analysis(_reply(), headline="bags order worth Rs 412 crore")
    parsed = AnalysisResponse.model_validate(mapped)     # the real gate
    assert parsed.event_type == "ORDER_WIN"
    assert parsed.recommendation == "BUY"
    assert parsed.sentiment == "positive"
    assert mapped["key_numbers"]["deal_value_inr_crore"] == 412.0
    assert raw["price_path"][14] == 26                   # raw survives intact


def test_no_move_is_a_hold_whatever_the_direction():
    """A predicted drift is not a reason to take a position."""
    for direction in ("UP", "DOWN", "FLAT"):
        mapped, _ = to_analysis(_reply(direction=direction, mover=False))
        assert mapped["recommendation"] == "HOLD"


def test_down_mover_is_a_sell():
    mapped, _ = to_analysis(_reply(direction="DOWN", mover=True,
                                   price_path=[-5] * 16 + [-9]))
    assert mapped["recommendation"] == "SELL"
    assert mapped["sentiment"] == "negative"
    assert mapped["sentiment_score"] < 0


def test_sentiment_score_reads_t15_and_saturates_at_3pct():
    path = [0] * 14 + [30, 0, 0]          # +3.0% at t15
    mapped, _ = to_analysis(_reply(price_path=path))
    assert mapped["sentiment_score"] == 100.0
    path = [0] * 14 + [15, 0, 0]          # +1.5%
    mapped, _ = to_analysis(_reply(price_path=path))
    assert mapped["sentiment_score"] == 50.0


def test_results_bucket_is_split_by_the_bots_own_headline_detector():
    """The model has one RESULTS class; the rules engine needs the quarter."""
    mapped, _ = to_analysis(
        _reply(event_type="RESULTS"),
        headline="Financial Results for the quarter ended June 30, 2026 (Q1)",
    )
    assert mapped["event_type"] in {
        "Q1_RESULTS", "Q2_RESULTS", "Q3_RESULTS", "Q4_RESULTS",
        "ANNUAL_RESULTS", "OTHER",
    }


def test_unknown_event_classes_collapse_to_other_not_a_crash():
    for et in ("INVESTOR_MEET", "TRADING_WINDOW", "RATING", "WAT", None):
        mapped, _ = to_analysis(_reply(event_type=et))
        AnalysisResponse.model_validate(mapped)


def test_confidence_is_driven_by_mover():
    high, _ = to_analysis(_reply(mover=True, materiality="HIGH"))
    low, _ = to_analysis(_reply(mover=False, materiality="LOW"))
    assert high["confidence"] > low["confidence"]
    assert 0.0 < low["confidence"] <= 1.0 and high["confidence"] <= 1.0


def test_fenced_json_is_tolerated():
    mapped, _ = to_analysis("```json\n" + _reply() + "\n```")
    assert mapped["recommendation"] == "BUY"


def test_garbage_raises_so_the_caller_stores_invalid_json():
    with pytest.raises(Exception):
        to_analysis("I think this filing is quite positive!")


def test_prompt_matches_the_trained_format():
    system, user = build_prompt(
        symbol="ONGC", filed_at="2026-04-04 08:27:39",
        headline="ONGC stabilises fire incident", filing_text="Press Release ...",
    )
    assert system == SYSTEM_PROMPT
    assert user.startswith("SYMBOL: ONGC\nFILED: 2026-04-04 08:27:39\nHEADLINE: ")
    assert "\nFILING:\n" in user


def test_prompt_falls_back_to_the_headline_when_the_pdf_failed():
    _, user = build_prompt(symbol="X", filed_at="t", headline="H", filing_text="")
    assert user.endswith("FILING:\nH")
