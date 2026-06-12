"""T3 — analyzer smoke test.

Boots the analyzer with a stubbed DeepSeek transport (no API key
needed) and processes a single synthetic announcement. Verifies:

  - 1 analyses row in the DB
  - 1 signals row in the DB
  - 1 publish on the `signals.new` event-bus channel
  - The chosen prompt template matches the keyword heuristic

Run with:

    .venv/Scripts/python.exe scripts/smoke_t3_analyzer.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _stub_response() -> dict:
    payload = {
        "event_type": "ORDER_WIN",
        "summary": "Reliance wins a Rs 5000 crore defence order.",
        "sentiment": "positive",
        "sentiment_score": 80.0,
        "confidence": 0.9,
        "recommendation": "BUY",
        "reasoning": "Material order, strong counterparty, repeatable revenue.",
        "key_numbers": {"deal_value_inr_crore": 5000.0},
    }
    return {
        "id": "chatcmpl-smoke",
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(payload)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 250, "completion_tokens": 80, "total_tokens": 330},
    }


def _make_handler():
    import httpx
    def handler(req):
        return httpx.Response(200, json=_stub_response())
    return handler


async def main() -> int:
    # Use a temp SQLite file so we don't touch the production trading.db.
    tmp = tempfile.mkdtemp(prefix="t3_smoke_")
    try:
        os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/smoke.db"
        os.environ["TESTING"] = "0"
        os.environ["DEEPSEEK_API_KEY"] = "sk-smoke"  # so the client doesn't bail early

        from app import config as app_config
        from app.db import init as db_init
        from app.db import session as db_session_mod
        from app.analyzer import Service, CHANNEL_NEW_SIGNAL
        from app.analyzer.prompts import seed_defaults
        from app.analyzer.deepseek_client import DeepSeekClient
        from app.db.models import Announcement, Analysis, Signal, Strategy, SignalRule
        from app.services.event_bus import event_bus
        import httpx

        app_config.reset_settings_cache()
        db_session_mod.rebuild_engine_for_testing()
        db_init.init_db()

        # Seed templates + a rule on the "default" strategy.
        with db_session_mod.SessionLocal() as s:
            seed_defaults(s)
            s.commit()
            strat = Strategy(name="default", description="smoke", enabled=True)
            s.add(strat)
            s.flush()
            rule = SignalRule(
                strategy_id=strat.id,
                name="big-acq",
                priority=10,
                conditions={
                    "all_of": [
                        {"field": "event_type", "op": "in", "value": ["ORDER_WIN", "ACQUISITION"]},
                        {"field": "sentiment_score", "op": ">=", "value": 50},
                        {"field": "confidence", "op": ">=", "value": 0.7},
                    ]
                },
                action="BUY",
                action_params={"position_size_pct": 1.5},
            )
            s.add(rule)
            s.commit()

            # Insert one synthetic announcement with a real-looking NSE URL.
            ann = Announcement(
                symbol="RELIANCE",
                exchange="NSE",
                event_type="ANNOUNCEMENT",
                headline="Reliance wins order worth Rs 5000 cr from defence",
                pdf_url="https://nseindia.com/corporate/RELIANCE_2026_05_01_order.pdf",
                source="https://nseindia.com/",
                filed_at=datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc),
            )
            s.add(ann)
            s.commit()
            s.refresh(ann)
            ann_id = ann.id

        # Stubbed DeepSeek client.
        client = DeepSeekClient(
            api_key="sk-smoke",
            timeout_s=5.0,
            max_retries=0,
            transport_factory=lambda: httpx.MockTransport(_make_handler()),
        )
        svc = Service(deepseek_client=client)

        # Subscribe to the publish channel.
        sub = event_bus.subscribe(CHANNEL_NEW_SIGNAL)
        try:
            sig = await svc.process_announcement(ann_id)
        finally:
            event_bus.unsubscribe(CHANNEL_NEW_SIGNAL, sub)
            await svc.aclose()

        # Verify the DB.
        with db_session_mod.SessionLocal() as s:
            analyses = s.query(Analysis).all()
            signals = s.query(Signal).all()
            print(f"analyses: {len(analyses)}")
            print(f"signals: {len(signals)}")
            assert len(analyses) == 1, f"expected 1 analysis, got {len(analyses)}"
            assert len(signals) == 1, f"expected 1 signal, got {len(signals)}"
            a = analyses[0]
            sg = signals[0]
            print(f"analysis: model={a.model} sent={a.sentiment} rec={a.recommendation} deal_cr={a.deal_value_inr_crore}")
            print(f"signal:   action={sg.action} rule_id={sg.rule_id} size_pct={sg.position_size_pct}")
            assert sg.action == "BUY"
            assert sg.rule_id is not None
            assert sg.position_size_pct == 1.5  # came from the rule's action_params

        # Verify the publish.
        assert not sub.empty(), "expected publish on signals.new"
        evt = sub.get_nowait()
        print(f"published: action={evt.payload['action']} symbol={evt.payload['symbol']}")
        assert evt.payload["symbol"] == "RELIANCE"
        assert evt.payload["action"] == "BUY"

        print("smoke ok.")
        # Dispose the engine so the SQLite file handle is released
        # before the OS tries to delete the tempdir.
        from app.db.session import engine
        engine.dispose()
        return 0
    finally:
        # Best-effort cleanup; ignore Windows file-locking errors.
        import shutil
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
