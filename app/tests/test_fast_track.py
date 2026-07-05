"""Tests for the deterministic fast track (app/analyzer/fast_track.py).

Parser units + service-level routing:
  - order win / buyback / KMP resignation headlines match with the
    right recommendation, confidence tiers, and key numbers;
  - ambiguous or guarded headlines (cancellations, routine succession,
    independent directors, valueless orders) do NOT match — they must
    fall through to the LLM track;
  - with FAST_TRACK_ENABLED the service produces a signal WITHOUT
    calling DeepSeek; with a non-matching headline the LLM track still
    runs; with the toggle OFF (default) the LLM track runs even for a
    matching headline.
"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.analyzer.fast_track import evaluate_fast_track, parse_inr_crore
from app.analyzer.service import Service
from app.db.models import Analysis, Signal
from app.tests.test_service import (
    _make_announcement,
    _make_deepseek_client,
    _seed_strategy_with_buy_rule,
    _deepseek_response,
    _valid_analysis_json,
)
from app.analyzer.prompts import seed_defaults


# -- parse_inr_crore --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("order worth Rs 450 crore", 450.0),
        ("order worth Rs. 1,234.56 crores", 1234.56),
        ("contract of ₹89.5 crore", 89.5),
        ("order valued at INR 300 crore", 300.0),
        ("order worth Rs 5000 cr", 5000.0),
        ("project of Rs 2 billion", 200.0),
        ("supply order of Rs 500 lakh", 5.0),
        ("order for Rs 750 million", 75.0),
        # Largest value wins (component + aggregate in one headline).
        ("orders of Rs 120 crore and Rs 330 crore aggregating Rs 450 crore", 450.0),
        # No currency marker → no value (tight by design).
        ("order worth 450 crore", None),
        ("routine announcement with no numbers", None),
    ],
)
def test_parse_inr_crore(text: str, expected: float | None) -> None:
    assert parse_inr_crore(text) == expected


# -- evaluate_fast_track: matches -------------------------------------------


def test_order_win_with_large_value_matches_buy() -> None:
    m = evaluate_fast_track(
        "ABC Infra Limited has bagged an order worth Rs 750 crore from NHAI"
    )
    assert m is not None
    assert m.pattern == "order_win_value"
    r = m.response
    assert str(r.recommendation) == "BUY"
    assert str(r.event_type) == "ORDER_WIN"
    assert r.confidence == 0.88  # >= 500 crore tier
    assert r.key_numbers.deal_value_inr_crore == 750.0
    # The cited fact is in the reasoning (auditable).
    assert "750" in r.reasoning


def test_order_win_confidence_tiers() -> None:
    small = evaluate_fast_track("wins order worth Rs 40 crore")
    mid = evaluate_fast_track("wins order worth Rs 200 crore")
    assert small is not None and small.response.confidence == 0.72
    assert mid is not None and mid.response.confidence == 0.80


def test_buyback_with_value_matches_buy() -> None:
    m = evaluate_fast_track(
        "Board approved buyback of equity shares aggregating Rs 1,100 crore"
    )
    assert m is not None
    assert m.pattern == "buyback_value"
    assert str(m.response.recommendation) == "BUY"
    assert m.response.key_numbers.buyback_value_inr_crore == 1100.0


def test_kmp_resignation_matches_sell() -> None:
    m = evaluate_fast_track(
        "Intimation regarding resignation of the Managing Director"
    )
    assert m is not None
    assert m.pattern == "kmp_resignation"
    assert str(m.response.recommendation) == "SELL"
    assert str(m.response.sentiment) == "negative"


# -- evaluate_fast_track: guarded non-matches (→ LLM track) -----------------


@pytest.mark.parametrize(
    "headline",
    [
        # Order guards.
        "Cancellation of order worth Rs 450 crore",
        "Termination of contract valued at Rs 300 crore",
        "Company participated in a bid for a Rs 900 crore tender",
        # No explicit value → LLM judges materiality.
        "Company wins a large order from a European customer",
        # Below the 25-crore floor.
        "Company secures order worth Rs 10 crore",
        # Routine succession / independent-director exits.
        "Resignation and appointment of Chief Financial Officer",
        "Resignation of Independent Director",
        # Non-KMP resignation.
        "Resignation of company secretary's assistant",
        # Buyback without a value.
        "Board meeting to consider buyback proposal",
        # Plain administrative noise.
        "Compliance certificate under Regulation 7(3)",
        "",
    ],
)
def test_fast_track_leaves_ambiguous_headlines_to_llm(headline: str) -> None:
    assert evaluate_fast_track(headline) is None


# -- Service routing ---------------------------------------------------------


def _llm_must_not_be_called(req: httpx.Request) -> httpx.Response:
    raise AssertionError("fast track must not call DeepSeek")


@pytest.mark.asyncio
async def test_service_fast_track_signals_without_llm(
    db_session, isolated_db, monkeypatch
) -> None:
    from app import config as app_config

    monkeypatch.setenv("FAST_TRACK_ENABLED", "1")
    app_config.reset_settings_cache()
    try:
        seed_defaults(db_session)
        _seed_strategy_with_buy_rule(db_session)
        db_session.commit()
        ann = _make_announcement(
            db_session, title="Reliance wins order worth Rs 5000 cr"
        )

        svc = Service(deepseek_client=_make_deepseek_client(_llm_must_not_be_called))
        try:
            signal = await svc.process_announcement(ann.id)
        finally:
            await svc.aclose()

        assert signal is not None
        assert signal.action == "BUY"
        analysis = db_session.execute(select(Analysis)).scalars().one()
        # Provenance: the analysis names the fast track, not the LLM.
        assert analysis.model == "fast-track"
        assert analysis.deal_value_inr_crore == 5000.0
        assert analysis.raw_response["timings"]["llm_ms"] == 0.0
    finally:
        app_config.reset_settings_cache()


@pytest.mark.asyncio
async def test_service_non_matching_headline_uses_llm_track(
    db_session, isolated_db, monkeypatch
) -> None:
    from app import config as app_config

    monkeypatch.setenv("FAST_TRACK_ENABLED", "1")
    app_config.reset_settings_cache()
    try:
        seed_defaults(db_session)
        _seed_strategy_with_buy_rule(db_session)
        db_session.commit()
        # No explicit value → the fast track must decline this one.
        ann = _make_announcement(
            db_session, title="Company update on business operations"
        )

        handler = lambda req: httpx.Response(
            200, json=_deepseek_response(_valid_analysis_json())
        )
        svc = Service(deepseek_client=_make_deepseek_client(handler))
        try:
            signal = await svc.process_announcement(ann.id)
        finally:
            await svc.aclose()

        assert signal is not None
        analysis = db_session.execute(select(Analysis)).scalars().one()
        assert analysis.model == "deepseek-chat"  # LLM track ran
    finally:
        app_config.reset_settings_cache()


@pytest.mark.asyncio
async def test_service_toggle_off_uses_llm_even_for_matching_headline(
    db_session, isolated_db, monkeypatch
) -> None:
    """Explicitly OFF: the LLM analyses order wins too, even though
    the new default is ON — the toggle must gate the fast track."""
    from app import config as app_config

    monkeypatch.setenv("FAST_TRACK_ENABLED", "0")
    app_config.reset_settings_cache()
    try:
        seed_defaults(db_session)
        _seed_strategy_with_buy_rule(db_session)
        db_session.commit()
        ann = _make_announcement(
            db_session, title="Reliance wins order worth Rs 5000 cr"
        )

        handler = lambda req: httpx.Response(
            200, json=_deepseek_response(_valid_analysis_json())
        )
        svc = Service(deepseek_client=_make_deepseek_client(handler))
        try:
            signal = await svc.process_announcement(ann.id)
        finally:
            await svc.aclose()

        assert signal is not None
        analysis = db_session.execute(select(Analysis)).scalars().one()
        assert analysis.model == "deepseek-chat"
    finally:
        app_config.reset_settings_cache()
