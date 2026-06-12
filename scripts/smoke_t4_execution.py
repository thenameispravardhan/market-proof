"""Smoke test for T4 — full pipeline:
    analyzer (T3) -> signal -> risk -> paper fill -> positions -> P&L

Run with:
    .venv/Scripts/python.exe scripts/smoke_t4_execution.py

No DEEPSEEK_API_KEY or FYERS sandbox token needed: the analyzer
runs against a stubbed DeepSeek transport, and the execution
layer uses the in-process PaperBackend. The smoke proves the full
T1+T2+T3+T4 wiring works end-to-end.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx

# Ensure the project root is on sys.path when run as a script.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Force test-friendly defaults BEFORE any app import.
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("FYERS_APP_ID", "")
os.environ.setdefault("FYERS_SECRET_KEY", "")
os.environ.setdefault("TRADING_MODE", "paper")
# If a previous run wrote a file-backed DB, drop it.
for p in ("./data/trading.db", "./trading.db"):
    if os.path.exists(p):
        os.remove(p)

from app.analyzer.deepseek_client import (
    DEFAULT_COST_PER_1K,
    DeepSeekClient,
)
from app.analyzer.prompts import seed_defaults
from app.analyzer.rules_engine import DEFAULT_STRATEGY_NAME
from app.analyzer.service import CHANNEL_NEW_SIGNAL, Service
from app.config import reset_settings_cache
from app.db import init as db_init
from app.db import session as db_session_mod
from app.db.models import (
    BrokerAccount,
    Position,
    RiskEvent,
    Signal,
    Strategy,
    SignalRule,
    Trade,
)
from app.db.session import SessionLocal
from app.execution.manager import CHANNEL_TRADE_EXECUTED, Manager
from app.execution.market_data import MarketDataBus
from app.execution.paper import PaperBackend
from app.risk.engine import RiskEngine
from app.services.event_bus import event_bus


# -- Stubbed DeepSeek transport -------------------------------------------


def _stub_deepseek(req: httpx.Request) -> httpx.Response:
    body = json.loads(req.content.decode("utf-8"))
    user = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
    if "BUYBACK" in user.upper():
        event_type = "BUYBACK"
    else:
        event_type = "ORDER_WIN"
    payload = {
        "event_type": event_type,
        "summary": "Reliance wins a Rs 5000 crore order.",
        "sentiment": "positive",
        "sentiment_score": 75.0,
        "confidence": 0.85,
        "recommendation": "BUY",
        "reasoning": "Material order, strong counterparty, repeatable business.",
        "key_numbers": {"deal_value_inr_crore": 5000.0},
    }
    return httpx.Response(200, json={
        "id": "chatcmpl-smoke",
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(payload)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280},
    })


# -- Helpers --------------------------------------------------------------


def _seed_db() -> int:
    """Seed prompt templates, a default strategy with a BUY rule,
    a paper broker account, and an Announcement. Returns the
    announcement id."""
    # Use one session for everything — the in-memory SQLite with
    # StaticPool keeps the connection, but having multiple
    # `with SessionLocal()` blocks confuses SQLAlchemy.
    s = SessionLocal()
    try:
        # Templates.
        seed_defaults(s)
        s.commit()
        # Strategy.
        s.add(Strategy(name=DEFAULT_STRATEGY_NAME, enabled=True, config={
            "max_capital_risk_pct": 0.05,
            "broker_account_id": None,  # filled below
        }))
        s.commit()
        strat = s.query(Strategy).filter_by(name=DEFAULT_STRATEGY_NAME).one()
        s.add(SignalRule(
            strategy_id=strat.id,
            name="smoke-buy",
            priority=10,
            conditions={
                "all_of": [
                    {"field": "event_type", "op": "in", "value": ["ORDER_WIN", "BUYBACK", "ACQUISITION"]},
                    {"field": "sentiment_score", "op": ">=", "value": 50},
                    {"field": "confidence", "op": ">=", "value": 0.7},
                ]
            },
            action="BUY",
            action_params={"position_size_pct": 5.0},
        ))
        # Paper broker account.
        s.add(BrokerAccount(
            name="smoke-paper", broker="fyers", paper_mode=True, enabled=True,
        ))
        s.commit()
        ba = s.query(BrokerAccount).filter_by(name="smoke-paper").one()
        strat.config = {
            "max_capital_risk_pct": 0.05,
            "broker_account_id": ba.id,
        }
        s.commit()
        # Announcement.
        from app.db.models import Announcement
        a = Announcement(
            symbol="RELIANCE",
            exchange="NSE",
            event_type="ANNOUNCEMENT",
            headline="Reliance wins Rs 5000 cr order",
            pdf_url="https://nseindia.com/RELIANCE_2026_05_01_order.pdf",
        )
        s.add(a)
        s.commit()
        s.refresh(a)
        return a.id
    finally:
        s.close()


# -- Main ----------------------------------------------------------------


async def main() -> int:
    # Reset settings + rebuild engine to make sure TESTING is honoured.
    reset_settings_cache()
    db_session_mod.rebuild_engine_for_testing()
    # Re-bind SessionLocal to the new engine — the imported symbol
    # was captured at import time before the rebuild.
    global SessionLocal
    SessionLocal = db_session_mod.SessionLocal
    db_init.init_db()
    # Sanity: ensure all tables are present.
    from sqlalchemy import inspect as _insp
    _tables = sorted(_insp(db_session_mod.engine).get_table_names())
    assert "prompt_templates" in _tables, f"prompt_templates missing: {_tables}"

    # Seed.
    ann_id = _seed_db()
    print(f"[smoke] seeded announcement id={ann_id}")

    # Build components.
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)

    # DeepSeek stub.
    ds = DeepSeekClient(
        api_key="sk-smoke",
        transport_factory=lambda: httpx.MockTransport(_stub_deepseek),
    )
    analyzer = Service(deepseek_client=ds)
    risk = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    paper = PaperBackend(market_data=md, session_factory=SessionLocal)
    mgr = Manager(risk_engine=risk, market_data=md, paper_backend=paper)

    # Wire event bus — manager subscribes via its own start().
    analyzer.start()
    mgr.start()
    try:
        await mgr.wait_until_ready(timeout=2.0)
        # Subscribe to trade.executed for the assertion.
        sub = event_bus.subscribe(CHANNEL_TRADE_EXECUTED)

        # Drive the analyzer.
        signal = await analyzer.process_announcement(ann_id)
        assert signal is not None
        print(f"[smoke] analyzer produced signal id={signal.id} action={signal.action} status={signal.status}")

        # Wait for the manager to consume the publish.
        deadline = time.time() + 10.0
        evt = None
        while time.time() < deadline:
            try:
                evt = sub.get_nowait()
                break
            except Exception:
                await asyncio.sleep(0.05)
        assert evt is not None, "trade.executed event never fired"
        payload = evt.payload
        print(f"[smoke] trade.executed: {payload}")

        # Verify the DB.
        with SessionLocal() as s:
            trades = s.query(Trade).all()
            positions = s.query(Position).all()
            risk_events = s.query(RiskEvent).all()
        assert len(trades) >= 1, "no trade row"
        assert any(t.status == "filled" for t in trades), "no filled trade"
        assert any(p.symbol == "RELIANCE" for p in positions), "no RELIANCE position"

        # The filled trade should have a P&L after we tick the price.
        await md.tick("RELIANCE", 2600.0)
        positions_now = await paper.get_positions()
        assert len(positions_now) == 1
        # qty=100, entry=2500, last=2600 -> P&L = 100 * 100 = 10000.
        assert positions_now[0].unrealized_pnl == 10000.0, f"P&L was {positions_now[0].unrealized_pnl}"
        print(f"[smoke] mark-to-market P&L: {positions_now[0].unrealized_pnl}")

        # P&L non-zero check (the spec requirement).
        if any(t.pnl is not None and t.pnl != 0 for t in trades):
            print(f"[smoke] closed trade P&L: {[t.pnl for t in trades]}")
        # P&L can be on unrealized position; that's the typical case
        # for a fresh open.

        # Sanity: no risk events.
        assert not any(r.event_type.startswith("RISK_") for r in risk_events), "risk events fired unexpectedly"

        print("[smoke] OK — full pipeline works end-to-end")
        return 0
    finally:
        mgr.stop()
        analyzer.stop()
        await mgr.wait_until_stopped()
        await analyzer.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
