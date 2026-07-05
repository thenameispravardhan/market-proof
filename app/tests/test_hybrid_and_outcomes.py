"""Tests for the hybrid fast track, the PDF prefetch cache, and the
Phase 4 outcome logger."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.analyzer import pdf_cache
from app.analyzer.fast_track import (
    evaluate_fast_track_text,
    is_hybrid_order_candidate,
)
from app.analyzer.service import Service
from app.db.models import Analysis, SignalOutcome
from app.services.event_bus import event_bus
from app.services.outcome_logger import CHANNEL_SIGNALS, OutcomeLogger
from app.tests.test_service import (
    _make_announcement,
    _make_deepseek_client,
    _seed_strategy_with_buy_rule,
)
from app.analyzer.prompts import seed_defaults


# -- hybrid candidate detection ----------------------------------------------


def test_hybrid_candidate_order_headline_without_value() -> None:
    assert is_hybrid_order_candidate(
        "HCL Technologies Limited has informed the Exchange about Bagging of order"
    )


def test_hybrid_candidate_rejects_valueful_and_guarded_headlines() -> None:
    # Value already in the headline → the normal headline parser owns it.
    assert not is_hybrid_order_candidate("wins order worth Rs 500 crore")
    # Cancellation guard.
    assert not is_hybrid_order_candidate("Cancellation of order by customer")
    # No order context at all.
    assert not is_hybrid_order_candidate("Board meeting intimation")
    assert not is_hybrid_order_candidate("")


# -- hybrid value parse from extracted text ----------------------------------


def test_hybrid_parses_value_near_order_context() -> None:
    headline = "Company has informed the Exchange regarding bagging of order"
    text = (
        "--- Page 1 of 3 ---\n"
        "We are pleased to inform that the Company has received an order\n"
        "worth Rs. 450 crore from a leading PSU for supply of equipment.\n"
    )
    m = evaluate_fast_track_text(headline, text)
    assert m is not None
    assert m.pattern == "order_win_pdf_value"
    assert m.response.key_numbers.deal_value_inr_crore == 450.0
    assert str(m.response.recommendation) == "BUY"
    assert m.response.confidence <= 0.85  # hybrid is capped below headline path


def test_hybrid_survives_line_broken_value() -> None:
    # PDF extraction breaks sentences across lines — the parse must
    # survive "order worth Rs\n450 crore".
    headline = "Update on order win"
    text = "the company secured an order worth Rs\n450 crore from the client"
    m = evaluate_fast_track_text(headline, text)
    assert m is not None
    assert m.response.key_numbers.deal_value_inr_crore == 450.0


def test_hybrid_ignores_values_far_from_order_context() -> None:
    # A revenue line in a results table must NOT be read as the order size.
    headline = "Intimation regarding receipt of order"
    filler = "x " * 400  # push the revenue line far from any order mention
    text = f"the company received an order from a customer. {filler} total income Rs 5,000 crore"
    m = evaluate_fast_track_text(headline, text)
    assert m is None  # no value near the order mention → LLM track


def test_hybrid_blocked_by_cancellation_in_text() -> None:
    headline = "Update regarding order"
    text = "order worth Rs 450 crore stands terminated by the customer"
    assert evaluate_fast_track_text(headline, text) is None


def test_hybrid_no_text_no_match() -> None:
    assert evaluate_fast_track_text("bagging of order", "") is None


# -- pdf prefetch cache -------------------------------------------------------


@pytest.mark.asyncio
async def test_prefetch_cache_serves_bytes_without_second_download(monkeypatch) -> None:
    pdf_cache.clear()
    calls = {"n": 0}

    async def fake_download(url, *, timeout_s=6.0, transport=None):
        calls["n"] += 1
        return b"%PDF fake"

    monkeypatch.setattr(pdf_cache, "download_pdf", fake_download)
    pdf_cache.prefetch("https://x/a.pdf")
    data = await pdf_cache.get_pdf("https://x/a.pdf")
    assert data == b"%PDF fake"
    assert calls["n"] == 1  # served from the prefetch task, no re-download
    pdf_cache.clear()


@pytest.mark.asyncio
async def test_get_pdf_falls_back_to_direct_download(monkeypatch) -> None:
    pdf_cache.clear()
    calls = {"n": 0}

    async def fake_download(url, *, timeout_s=6.0, transport=None):
        calls["n"] += 1
        return b"%PDF direct"

    monkeypatch.setattr(pdf_cache, "download_pdf", fake_download)
    data = await pdf_cache.get_pdf("https://x/never-prefetched.pdf")
    assert data == b"%PDF direct"
    assert calls["n"] == 1
    pdf_cache.clear()


# -- service: hybrid end-to-end (no LLM) --------------------------------------


def _llm_must_not_be_called(req: httpx.Request) -> httpx.Response:
    raise AssertionError("hybrid fast track must not call DeepSeek")


@pytest.mark.asyncio
async def test_service_hybrid_order_signal_without_llm(
    db_session, isolated_db, monkeypatch
) -> None:
    from app import config as app_config

    monkeypatch.setenv("FAST_TRACK_ENABLED", "1")
    # SEND_EXTRACTED_TEXT stays OFF — hybrid must fetch the PDF anyway.
    monkeypatch.delenv("SEND_EXTRACTED_TEXT", raising=False)
    app_config.reset_settings_cache()

    async def fake_get_pdf(url, *, timeout_s=6.0):
        return b"%PDF fake"

    def fake_extract(pdf_bytes, *, max_chars=24_000):
        from app.analyzer.pdf_extract import ExtractionResult
        return ExtractionResult(
            ok=True,
            text="the company has received an order worth Rs 900 crore from NHAI",
            pages_total=2,
            pages_used=[1],
            chars=60,
        )

    monkeypatch.setattr("app.analyzer.service.pdf_cache.get_pdf", fake_get_pdf)
    monkeypatch.setattr("app.analyzer.service.extract_text_for_llm", fake_extract)
    try:
        seed_defaults(db_session)
        _seed_strategy_with_buy_rule(db_session)
        db_session.commit()
        ann = _make_announcement(
            db_session,
            title="Company has informed the Exchange regarding Bagging of order",
        )

        svc = Service(deepseek_client=_make_deepseek_client(_llm_must_not_be_called))
        try:
            signal = await svc.process_announcement(ann.id)
        finally:
            await svc.aclose()

        assert signal is not None
        assert signal.action == "BUY"
        analysis = db_session.execute(select(Analysis)).scalars().one()
        assert analysis.model == "fast-track"
        assert analysis.deal_value_inr_crore == 900.0
        # Extraction provenance is stored even though SEND_EXTRACTED_TEXT is off.
        assert analysis.raw_response["pdf_extraction"]["ok"] is True
    finally:
        app_config.reset_settings_cache()


# -- outcome logger ------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcome_logger_records_baseline_and_samples(
    db_session, isolated_db
) -> None:
    prices = iter([100.0, 102.0, 95.0])  # baseline, +5m, +30m

    async def fake_quote(symbol: str):
        return next(prices, None)

    logger = OutcomeLogger(
        quote_fn=fake_quote,
        sample_points=(("5m", 0.03), ("30m", 0.06)),
    )
    logger.start()
    await asyncio.sleep(0.05)  # let it subscribe
    await event_bus.publish(
        CHANNEL_SIGNALS,
        {"signal_id": 1, "symbol": "RELIANCE", "action": "BUY", "status": "pending"},
    )
    await asyncio.sleep(0.5)  # baseline + both samples
    logger.stop()
    await logger.wait_until_stopped()

    row = db_session.execute(select(SignalOutcome)).scalars().one()
    assert row.symbol == "RELIANCE"
    assert row.price_at_signal == 100.0
    assert row.price_5m == 102.0
    assert row.move_5m_pct == 2.0
    assert row.price_30m == 95.0
    assert row.move_30m_pct == -5.0
    assert row.note == "ok"
    assert row.completed_at is not None


@pytest.mark.asyncio
async def test_outcome_logger_tags_missing_quotes(db_session, isolated_db) -> None:
    async def no_quote(symbol: str):
        return None

    logger = OutcomeLogger(
        quote_fn=no_quote,
        sample_points=(("5m", 0.02),),
    )
    logger.start()
    await asyncio.sleep(0.05)
    await event_bus.publish(
        CHANNEL_SIGNALS,
        {"signal_id": 2, "symbol": "TCS", "action": "SELL", "status": "blocked"},
    )
    await asyncio.sleep(0.3)
    logger.stop()
    await logger.wait_until_stopped()

    row = db_session.execute(select(SignalOutcome)).scalars().one()
    assert row.price_at_signal is None
    assert "no_quote_at_signal" in (row.note or "")
    assert "no_quote_5m" in (row.note or "")


@pytest.mark.asyncio
async def test_outcome_logger_respects_toggle_off(
    db_session, isolated_db, monkeypatch
) -> None:
    from app import config as app_config

    monkeypatch.setenv("OUTCOME_LOGGER_ENABLED", "0")
    app_config.reset_settings_cache()
    try:
        async def fake_quote(symbol: str):
            return 100.0

        logger = OutcomeLogger(quote_fn=fake_quote, sample_points=(("5m", 0.02),))
        logger.start()
        await asyncio.sleep(0.05)
        await event_bus.publish(
            CHANNEL_SIGNALS,
            {"signal_id": 3, "symbol": "INFY", "action": "BUY", "status": "pending"},
        )
        await asyncio.sleep(0.2)
        logger.stop()
        await logger.wait_until_stopped()

        rows = db_session.execute(select(SignalOutcome)).scalars().all()
        assert rows == []
    finally:
        app_config.reset_settings_cache()
