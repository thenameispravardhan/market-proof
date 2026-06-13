"""Schema validation tests.

The DeepSeek response is validated by `AnalysisResponse`. These tests
cover the full range of valid inputs, the boundary checks on
`sentiment_score` (-100..100) and `confidence` (0..1), the required
fields, and the `analysis_to_dict` helper.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.analyzer.schemas import (
    DEFAULT_EVENT_TYPE,
    EVENT_TYPES,
    AnalysisResponse,
    EventType,
    KeyNumbers,
    Recommendation,
    Sentiment,
    analysis_to_dict,
)


# -- Fixtures ------------------------------------------------------------


def _valid_payload(**overrides):
    base = {
        "event_type": "ORDER_WIN",
        "summary": "Reliance wins a Rs 5000 crore order.",
        "sentiment": "positive",
        "sentiment_score": 75.0,
        "confidence": 0.85,
        "recommendation": "BUY",
        "reasoning": "Material order, strong counterparty, repeatable business.",
        "key_numbers": {
            "deal_value_inr_crore": 5000.0,
        },
    }
    base.update(overrides)
    return base


# -- Enum sanity ---------------------------------------------------------


def test_event_types_has_15_plus_default():
    assert len(EVENT_TYPES) == 16
    assert DEFAULT_EVENT_TYPE in EVENT_TYPES
    assert EVENT_TYPES[-1] == DEFAULT_EVENT_TYPE
    # Every value is a real EventType.
    for v in EVENT_TYPES:
        if v == DEFAULT_EVENT_TYPE:
            continue
        assert EventType(v).value == v


def test_event_type_enum_has_15_values():
    assert len(EventType) == 15


# -- Happy path ----------------------------------------------------------


def test_valid_response_parses():
    a = AnalysisResponse.model_validate(_valid_payload())
    assert a.event_type == EventType.ORDER_WIN
    assert a.sentiment == Sentiment.POSITIVE
    assert a.recommendation == Recommendation.BUY
    assert a.confidence == 0.85
    assert a.sentiment_score == 75.0
    assert a.key_numbers.deal_value_inr_crore == 5000.0


def test_all_15_event_types_round_trip():
    """Every EventType value parses successfully via the pydantic
    enum coercion."""
    for et in EventType:
        a = AnalysisResponse.model_validate(_valid_payload(event_type=et.value))
        assert a.event_type == et


def test_three_sentiments_round_trip():
    for s in ("positive", "neutral", "negative"):
        a = AnalysisResponse.model_validate(_valid_payload(sentiment=s))
        assert a.sentiment == Sentiment(s)


def test_three_recommendations_round_trip():
    for r in ("BUY", "SELL", "HOLD"):
        a = AnalysisResponse.model_validate(_valid_payload(recommendation=r))
        assert a.recommendation == Recommendation(r)


def test_enum_fields_are_case_insensitive():
    """Live DeepSeek responses use inconsistent casing ("buy",
    "Positive", "buyback"); the schema must normalise, not reject."""
    a = AnalysisResponse.model_validate(
        _valid_payload(
            event_type="buyback",
            sentiment="Positive",
            recommendation="buy",
        )
    )
    assert a.event_type == EventType.BUYBACK
    assert a.sentiment == Sentiment.POSITIVE
    assert a.recommendation == Recommendation.BUY


def test_fractional_sentiment_score_is_scaled():
    # DeepSeek often returns the score on a 0..1 scale; it must be
    # lifted onto -100..100 so the rules engine sees a real magnitude.
    a = AnalysisResponse.model_validate(_valid_payload(sentiment_score=0.95))
    assert a.sentiment_score == pytest.approx(95.0)
    b = AnalysisResponse.model_validate(_valid_payload(sentiment_score=-0.8))
    assert b.sentiment_score == pytest.approx(-80.0)
    # Genuine -100..100 values pass through unchanged.
    c = AnalysisResponse.model_validate(_valid_payload(sentiment_score=72))
    assert c.sentiment_score == pytest.approx(72.0)
    # Exactly zero stays neutral.
    d = AnalysisResponse.model_validate(_valid_payload(sentiment_score=0))
    assert d.sentiment_score == pytest.approx(0.0)


def test_unknown_event_type_maps_to_other():
    # The LLM sometimes invents categories like FUND_RAISING /
    # APPOINTMENT that aren't in our 15-value enum. These must NOT
    # reject the whole analysis — they degrade to OTHER, keeping the
    # (valid) sentiment + recommendation usable for trading.
    a = AnalysisResponse.model_validate(
        _valid_payload(event_type="FUND_RAISING", recommendation="HOLD")
    )
    assert a.event_type == EventType.OTHER
    assert a.recommendation == Recommendation.HOLD


def test_key_numbers_all_optional():
    a = AnalysisResponse.model_validate(
        _valid_payload(key_numbers={})
    )
    kn = a.key_numbers
    assert kn.deal_value_inr_crore is None
    assert kn.stake_change_pct is None
    assert kn.dividend_per_share is None
    assert kn.buyback_value_inr_crore is None


def test_key_numbers_partial_fill():
    a = AnalysisResponse.model_validate(
        _valid_payload(
            key_numbers={"dividend_per_share": 12.5},
        )
    )
    assert a.key_numbers.dividend_per_share == 12.5
    assert a.key_numbers.deal_value_inr_crore is None


def test_extra_keys_in_payload_ignored():
    """LLMs sometimes add extra keys. We ignore them, not raise."""
    p = _valid_payload()
    p["some_extra"] = "ignored"
    p["key_numbers"]["yet_another"] = 99
    a = AnalysisResponse.model_validate(p)
    assert a.event_type == EventType.ORDER_WIN


def test_analysis_to_dict_shape():
    a = AnalysisResponse.model_validate(_valid_payload())
    d = analysis_to_dict(a)
    assert d["event_type"] == "ORDER_WIN"
    assert d["sentiment"] == "positive"
    assert d["sentiment_score"] == 75.0
    assert d["confidence"] == 0.85
    assert d["recommendation"] == "BUY"
    assert isinstance(d["key_numbers"], dict)
    assert d["key_numbers"]["deal_value_inr_crore"] == 5000.0


# -- Range / boundary checks --------------------------------------------


def test_confidence_zero_ok():
    a = AnalysisResponse.model_validate(_valid_payload(confidence=0.0))
    assert a.confidence == 0.0


def test_confidence_one_ok():
    a = AnalysisResponse.model_validate(_valid_payload(confidence=1.0))
    assert a.confidence == 1.0


def test_confidence_negative_rejected():
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(_valid_payload(confidence=-0.1))


def test_confidence_above_one_rejected():
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(_valid_payload(confidence=1.1))


def test_sentiment_score_minus_100_ok():
    a = AnalysisResponse.model_validate(_valid_payload(sentiment_score=-100.0))
    assert a.sentiment_score == -100.0


def test_sentiment_score_plus_100_ok():
    a = AnalysisResponse.model_validate(_valid_payload(sentiment_score=100.0))
    assert a.sentiment_score == 100.0


def test_sentiment_score_above_100_rejected():
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(_valid_payload(sentiment_score=100.1))


def test_sentiment_score_below_minus_100_rejected():
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(_valid_payload(sentiment_score=-100.1))


# -- Required fields -----------------------------------------------------


def test_missing_event_type_rejected():
    p = _valid_payload()
    del p["event_type"]
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(p)


def test_missing_summary_rejected():
    p = _valid_payload()
    del p["summary"]
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(p)


def test_empty_summary_rejected():
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(_valid_payload(summary=""))


def test_whitespace_summary_rejected():
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(_valid_payload(summary="   "))


def test_missing_reasoning_rejected():
    p = _valid_payload()
    del p["reasoning"]
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(p)


def test_missing_key_numbers_rejected():
    p = _valid_payload()
    del p["key_numbers"]
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(p)


# -- Enum invalid values ------------------------------------------------


def test_invalid_event_type_coerced_to_other():
    # Unknown event types degrade to OTHER rather than raising, so a
    # novel LLM label never throws away a valid analysis.
    a = AnalysisResponse.model_validate(_valid_payload(event_type="BOGUS_TYPE"))
    assert a.event_type == EventType.OTHER


def test_invalid_sentiment_rejected():
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(_valid_payload(sentiment="happy"))


def test_invalid_recommendation_rejected():
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(_valid_payload(recommendation="YOLO"))


# -- to_db_columns -------------------------------------------------------


def test_to_db_columns_maps_to_orm_columns():
    a = AnalysisResponse.model_validate(_valid_payload())
    cols = a.to_db_columns()
    expected = {
        "event_type",
        "summary",
        "sentiment",
        "sentiment_score",
        "confidence",
        "recommendation",
        "rationale",
        "deal_value_inr_crore",
        "stake_change_pct",
        "dividend_per_share",
        "buyback_value_inr_crore",
    }
    assert set(cols.keys()) == expected
    assert cols["rationale"] == a.reasoning
    assert cols["summary"] == a.summary
