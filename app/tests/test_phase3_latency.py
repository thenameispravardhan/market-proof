"""Phase 3 latency-control tests.

  - PIPELINE_DEADLINE_SECONDS: a fully-analysed announcement whose age
    exceeds the deadline still gets its analysis stored, but the signal
    is BLOCKED with reason pipeline_deadline. 0 (default) = disabled.
  - LLM retry policy: DeepSeekClient defaults come from settings
    (LLM_MAX_RETRIES), replacing the old fixed 3-retry ladder.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.analyzer.deepseek_client import DeepSeekClient, DeepSeekError
from app.analyzer.prompts import seed_defaults
from app.analyzer.service import Service
from app.db.models import Analysis, Signal
from app.tests.test_service import (
    _deepseek_response,
    _make_announcement,
    _make_deepseek_client,
    _seed_strategy_with_buy_rule,
    _valid_analysis_json,
)


# -- pipeline deadline -------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_deadline_blocks_late_signal_but_keeps_analysis(
    db_session, isolated_db, monkeypatch
) -> None:
    from app import config as app_config

    monkeypatch.setenv("PIPELINE_DEADLINE_SECONDS", "5")
    app_config.reset_settings_cache()
    try:
        seed_defaults(db_session)
        _seed_strategy_with_buy_rule(db_session)
        db_session.commit()
        # 10s old: passes the 90s staleness gate, fails the 5s deadline.
        ann = _make_announcement(
            db_session,
            filed_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )

        handler = lambda req: httpx.Response(
            200, json=_deepseek_response(_valid_analysis_json())
        )
        svc = Service(deepseek_client=_make_deepseek_client(handler))
        try:
            signal = await svc.process_announcement(ann.id)
        finally:
            await svc.aclose()

        # The analysis is kept (training data)…
        analysis = db_session.execute(select(Analysis)).scalars().one()
        assert analysis.model == "deepseek-chat"
        assert analysis.recommendation == "BUY"
        # …but the trade is blocked with the deadline reason.
        assert signal is not None
        assert signal.status == "blocked"
        assert signal.position_size_pct == 0.0
        assert "pipeline_deadline" in (signal.rationale or "")
    finally:
        app_config.reset_settings_cache()


@pytest.mark.asyncio
async def test_pipeline_deadline_disabled_by_default(
    db_session, isolated_db
) -> None:
    """Default 0 = off: the same 10s-old announcement trades normally."""
    seed_defaults(db_session)
    _seed_strategy_with_buy_rule(db_session)
    db_session.commit()
    ann = _make_announcement(
        db_session,
        filed_at=datetime.now(timezone.utc) - timedelta(seconds=10),
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
    assert signal.status == "pending"
    assert "pipeline_deadline" not in (signal.rationale or "")


# -- retry policy ------------------------------------------------------------


def _counting_500_handler():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    return handler, calls


@pytest.mark.asyncio
async def test_client_default_retries_come_from_settings(monkeypatch) -> None:
    from app import config as app_config

    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    app_config.reset_settings_cache()
    try:
        handler, calls = _counting_500_handler()
        client = DeepSeekClient(
            api_key="sk-test",
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        with pytest.raises(DeepSeekError):
            await client.complete(system="s", user="u")
        await client.aclose()
        # LLM_MAX_RETRIES=0 → exactly one attempt, no retry ladder.
        assert calls["n"] == 1
    finally:
        app_config.reset_settings_cache()


@pytest.mark.asyncio
async def test_client_single_fast_retry(monkeypatch) -> None:
    from app import config as app_config

    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_RETRY_BACKOFF_SECONDS", "0.01")
    app_config.reset_settings_cache()
    try:
        handler, calls = _counting_500_handler()
        client = DeepSeekClient(
            api_key="sk-test",
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        with pytest.raises(DeepSeekError):
            await client.complete(system="s", user="u")
        await client.aclose()
        # 1 original + 1 retry.
        assert calls["n"] == 2
    finally:
        app_config.reset_settings_cache()


@pytest.mark.asyncio
async def test_explicit_max_retries_still_wins(monkeypatch) -> None:
    """Tests / callers that pass max_retries explicitly are unaffected
    by the settings default."""
    from app import config as app_config

    monkeypatch.setenv("LLM_MAX_RETRIES", "5")
    app_config.reset_settings_cache()
    try:
        handler, calls = _counting_500_handler()
        client = DeepSeekClient(
            api_key="sk-test",
            max_retries=0,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        with pytest.raises(DeepSeekError):
            await client.complete(system="s", user="u")
        await client.aclose()
        assert calls["n"] == 1
    finally:
        app_config.reset_settings_cache()
