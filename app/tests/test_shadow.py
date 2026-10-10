"""SLM shadow mode and the MARKET CONTEXT fix.

The shadow path runs next to live trading, so the properties pinned here
are the safety ones (bounded, never raises, records failures) plus the one
that makes it useful: paired rows the comparison endpoint can score."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.analyzer import shadow as sh
from app.analyzer import slm_adapter
from app.db import session as dbs
from app.db.models import ShadowAnalysis
from app.research import stats
from app.tests.factories import seed_signal_chain

GOOD = json.dumps({"event_type": "ORDER_WIN", "materiality": "HIGH", "surprise": "MEDIUM",
                   "facts": {"amount_inr_cr": 412.0}, "direction": "UP", "mover": True,
                   "shape": "IMMEDIATE", "price_path": [3] * 17})


def test_session_labels_match_the_training_corpus():
    utc = lambda y, m, d, h, mi: datetime(y, m, d, h, mi, tzinfo=timezone.utc)  # noqa: E731
    assert slm_adapter.session_of(utc(2026, 9, 14, 5, 0)) == "same_session"      # 10:30 IST Mon
    assert slm_adapter.session_of(utc(2026, 9, 14, 3, 0)) == "same_day_preopen"  # 08:30 IST
    assert slm_adapter.session_of(utc(2026, 9, 14, 11, 0)) == "next_session"     # 16:30 IST
    assert slm_adapter.session_of(utc(2026, 9, 13, 5, 0)) == "next_session"      # Sunday
    assert slm_adapter.session_of(None) is None


def test_market_context_block_carries_only_known_fields():
    _, user = slm_adapter.build_prompt(symbol="ACME", filed_at="t", headline="h", filing_text="body",
                                       session="same_session", last_trade=123.456, cap_tier="small")
    assert "MARKET CONTEXT (as of the filing timestamp):" in user
    assert "  last_trade: 123.46" in user and "  cap_tier: small" in user
    assert "volume" not in user                       # unknown -> omitted, never faked
    assert user.index("MARKET CONTEXT") < user.index("FILING:")
    _, bare = slm_adapter.build_prompt(symbol="ACME", filed_at="t", headline="h", filing_text="b")
    assert "MARKET CONTEXT" not in bare


class FakeClient:
    def __init__(self, content=GOOD, delay=0.0, exc=None):
        self.content, self.delay, self.exc = content, delay, exc
        self.calls = []

    async def complete(self, **kw):
        self.calls.append(kw)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return SimpleNamespace(content=self.content)


def _settings(monkeypatch, **over):
    base = dict(LLM_SHADOW_ENABLED=True, LLM_PROVIDER="deepseek", LLM_SLM_ENDPOINT="http://spark:8000/v1/chat/completions",
                LLM_SLM_MODEL="tradebot-slm-v1", LLM_SLM_API_KEY="", LLM_MAX_TOKENS=300,
                LLM_SHADOW_TIMEOUT_SECONDS=5.0, CAP_LARGE_MIN_CR=50000.0, CAP_MID_MIN_CR=15000.0)
    base.update(over)
    monkeypatch.setattr(sh, "get_settings", lambda: SimpleNamespace(**base))


def _schedule(scorer, **kw):
    args = dict(announcement_id=1, analysis_id=None, signal_id=None, symbol="ACME", headline="Order win",
                filed_at=datetime(2026, 9, 14, 5, 0), filing_text="Received order worth Rs 412 crore",
                pdf_url="")
    args.update(kw)
    return scorer.schedule(**args)


def _rows():
    with dbs.SessionLocal() as db:
        return db.execute(select(ShadowAnalysis).order_by(ShadowAnalysis.id)).scalars().all()


@pytest.mark.asyncio
async def test_shadow_records_the_slm_verdict(monkeypatch, isolated_db):
    _settings(monkeypatch)
    client = FakeClient()
    scorer = sh.ShadowScorer(client_factory=lambda e, k: client)
    assert _schedule(scorer)
    await scorer.drain()
    [row] = _rows()
    assert (row.status, row.mover, row.direction, row.recommendation) == ("ok", True, "UP", "BUY")
    assert row.model == "tradebot-slm-v1" and row.latency_ms is not None
    assert "session: same_session" in client.calls[0]["user"]


@pytest.mark.asyncio
async def test_failures_are_rows_not_exceptions(monkeypatch, isolated_db):
    _settings(monkeypatch, LLM_SHADOW_TIMEOUT_SECONDS=0.01)
    for client in (FakeClient(content="not json"), FakeClient(exc=RuntimeError("endpoint down")),
                   FakeClient(delay=0.5)):
        scorer = sh.ShadowScorer(client_factory=lambda e, k, c=client: c)
        _schedule(scorer)
        await scorer.drain()
    assert [r.status for r in _rows()] == ["error", "error", "timeout"]
    assert "invalid_json" in _rows()[0].error


@pytest.mark.asyncio
async def test_disabled_or_primary_slm_schedules_nothing(monkeypatch, isolated_db):
    _settings(monkeypatch, LLM_SHADOW_ENABLED=False)
    assert not _schedule(sh.ShadowScorer(client_factory=lambda e, k: FakeClient()))
    _settings(monkeypatch, LLM_PROVIDER="slm")
    assert not _schedule(sh.ShadowScorer(client_factory=lambda e, k: FakeClient()))
    _settings(monkeypatch, LLM_SLM_ENDPOINT=" ")
    assert not _schedule(sh.ShadowScorer(client_factory=lambda e, k: FakeClient()))


@pytest.mark.asyncio
async def test_capacity_bound_skips_instead_of_queueing(monkeypatch, isolated_db):
    _settings(monkeypatch)
    monkeypatch.setattr(sh, "MAX_PENDING", 3)
    scorer = sh.ShadowScorer(client_factory=lambda e, k: FakeClient(delay=0.05))
    accepted = [_schedule(scorer) for _ in range(5)]
    assert accepted == [True, True, True, False, False] and scorer.skipped == 2
    await scorer.drain()
    assert len(_rows()) == 3


def test_paired_auc_difference_detects_a_better_model():
    import random

    rng = random.Random(5)
    y = [rng.random() < 0.3 for _ in range(400)]
    weak = [rng.random() for _ in y]
    strong = [(0.6 if t else 0.4) + rng.gauss(0, 0.1) for t in y]
    res = stats.paired_auc_difference(weak, strong, [int(t) for t in y])
    assert res["diff"] > 0.2 and res["ci_excludes_zero"]
    same = stats.paired_auc_difference(weak, weak, [int(t) for t in y])
    assert same["diff"] == 0.0 and not same["ci_excludes_zero"]


def test_shadow_endpoint_pairs_live_and_shadow_on_outcomes(client, db_session, isolated_db):
    # Live confidence is anti-predictive; the shadow model separates movers.
    for i in range(40):
        moved = i % 2 == 0
        chain = seed_signal_chain(db_session, symbol=f"S{i}", confidence=0.3 if moved else 0.9,
                                  move_30m=3.0 if moved else 0.1, move_5m=0.0)
        db_session.add(ShadowAnalysis(analysis_id=chain["analysis"].id, signal_id=chain["signal"].id,
                                      model="tradebot-slm-v1", status="ok", confidence=0.9 if moved else 0.3,
                                      recommendation="BUY", latency_ms=1500.0 + i))
    db_session.add(ShadowAnalysis(model="tradebot-slm-v1", status="timeout"))
    db_session.commit()
    body = client.get("/api/research/shadow", params={"days": 7}).json()
    assert body["paired"] == 40
    assert body["shadow_calls"] == {"ok": 40, "timeout": 1}
    cmp = body["auc_live_vs_shadow"]
    assert cmp["auc_a"] == 0.0 and cmp["auc_b"] == 1.0 and cmp["ci_excludes_zero"]
    assert body["shadow_latency_ms"]["p50"] is not None


def test_calibration_reports_by_month(client, db_session, isolated_db):
    for i in range(10):
        seed_signal_chain(db_session, symbol=f"M{i}", confidence=0.8, move_30m=2.0 if i < 5 else 0.0)
    body = client.get("/api/research/calibration", params={"days": 7}).json()
    assert len(body["by_month"]) == 1 and body["by_month"][0]["n"] == 10
