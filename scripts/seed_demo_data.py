#!/usr/bin/env python
"""Seed the local SQLite database with demo data for UI development.

Usage:
    python scripts/seed_demo_data.py

The script is idempotent: it uses upsert-style inserts and skips rows
that already exist.  Run it from the project root.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is on sys.path when run directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.init import init_db
from app.db.models import (
    Analysis,
    Announcement,
    BacktestRun,
    BrokerAccount,
    NotificationChannel,
    Position,
    PromptHistory,
    PromptTemplate,
    Signal,
    Strategy,
    SignalRule,
    Trade,
    Webhook,
)
from app.db.session import SessionLocal


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # SQLite-safe naive UTC


def seed(db) -> None:  # noqa: C901
    """Insert demo rows.  Each block checks for existing data first."""

    # ── Strategies ───────────────────────────────────────────────────────
    strategies_data = [
        {
            "name": "Aggressive Earnings",
            "description": "Buy on strong positive earnings with high confidence",
            "enabled": True,
            "config": {"max_single_position_pct": 15},
        },
        {
            "name": "Conservative Dividends",
            "description": "Small positions on dividend announcements only",
            "enabled": True,
            "config": {"max_single_position_pct": 5},
        },
    ]
    existing_strategies = {s.name for s in db.query(Strategy).all()}
    strategy_map: dict[str, Strategy] = {s.name: s for s in db.query(Strategy).all()}
    for sd in strategies_data:
        if sd["name"] not in existing_strategies:
            s = Strategy(**sd)
            db.add(s)
            db.flush()
            strategy_map[sd["name"]] = s
    db.flush()

    # ── Signal Rules ─────────────────────────────────────────────────────
    s_aggressive = strategy_map.get("Aggressive Earnings")
    s_conservative = strategy_map.get("Conservative Dividends")

    rules_data = []
    if s_aggressive:
        rules_data += [
            {
                "strategy_id": s_aggressive.id,
                "name": "High confidence earnings BUY",
                "priority": 10,
                "conditions": {
                    "all_of": [
                        {"field": "sentiment", "op": "==", "value": "positive"},
                        {"field": "confidence", "op": ">=", "value": 0.75},
                        {"field": "sentiment_score", "op": ">=", "value": 60},
                    ]
                },
                "action": "BUY",
                "action_params": {"position_size_pct": 10},
                "enabled": True,
            },
            {
                "strategy_id": s_aggressive.id,
                "name": "Negative earnings SELL",
                "priority": 20,
                "conditions": {
                    "all_of": [
                        {"field": "sentiment", "op": "==", "value": "negative"},
                        {"field": "confidence", "op": ">=", "value": 0.7},
                    ]
                },
                "action": "SELL",
                "action_params": {},
                "enabled": True,
            },
        ]
    if s_conservative:
        rules_data += [
            {
                "strategy_id": s_conservative.id,
                "name": "Dividend BUY small",
                "priority": 10,
                "conditions": {
                    "all_of": [
                        {"field": "event_type", "op": "==", "value": "DIVIDEND"},
                        {"field": "dividend_per_share", "op": ">=", "value": 5},
                    ]
                },
                "action": "BUY",
                "action_params": {"position_size_pct": 3},
                "enabled": True,
            },
        ]

    existing_rules = {(r.strategy_id, r.name) for r in db.query(SignalRule).all()}
    for rd in rules_data:
        key = (rd["strategy_id"], rd["name"])
        if key not in existing_rules:
            db.add(SignalRule(**rd))
    db.flush()

    # ── Broker Account ────────────────────────────────────────────────────
    existing_accounts = {a.name for a in db.query(BrokerAccount).all()}
    if "Paper Account" not in existing_accounts:
        db.add(BrokerAccount(
            name="Paper Account",
            broker="fyers",
            app_id="DEMO-APP-ID",
            secret_key="DEMO-SECRET",
            paper_mode=True,
            enabled=True,
        ))

    # ── Notification Channel ─────────────────────────────────────────────
    existing_channels = {c.name for c in db.query(NotificationChannel).all()}
    if "Demo Telegram" not in existing_channels:
        db.add(NotificationChannel(
            name="Demo Telegram",
            kind="telegram",
            config={"bot_token": "DEMO_TOKEN", "chat_id": "123456789"},
            events_filter="signal,risk_halt",
            enabled=False,  # demo — not actually connected
        ))

    # ── Webhook ───────────────────────────────────────────────────────────
    existing_webhooks = {w.name for w in db.query(Webhook).all()}
    if "Demo Outbound" not in existing_webhooks:
        db.add(Webhook(
            name="Demo Outbound",
            direction="out",
            url="https://httpbin.org/post",
            event_filter="signal",
            enabled=False,
        ))

    # ── Announcements ─────────────────────────────────────────────────────
    existing_announcements = {a.headline for a in db.query(Announcement).all()}
    announcements_data = [
        {
            "symbol": "RELIANCE",
            "exchange": "BSE",
            "event_type": "QUARTERLY_RESULT",
            "headline": "Reliance Industries Q4 FY26 Net Profit up 18% YoY",
            "body": "Revenue from operations at ₹2.4 lakh crore, up 12% YoY. EBITDA margins expanded 120 bps.",
            "pdf_url": "https://example.com/reliance-q4-fy26.pdf",
            "source": "BSE",
            "filed_at": utcnow() - timedelta(hours=2),
        },
        {
            "symbol": "TCS",
            "exchange": "NSE",
            "event_type": "DIVIDEND",
            "headline": "TCS declares ₹29/share final dividend for FY26",
            "pdf_url": "https://example.com/tcs-dividend.pdf",
            "source": "NSE",
            "filed_at": utcnow() - timedelta(hours=6),
        },
        {
            "symbol": "INFY",
            "exchange": "BSE",
            "event_type": "ACQUISITION",
            "headline": "Infosys acquires German fintech startup for €120M",
            "pdf_url": "https://example.com/infy-acquisition.pdf",
            "source": "BSE",
            "filed_at": utcnow() - timedelta(hours=12),
        },
        {
            "symbol": "HDFC",
            "exchange": "BSE",
            "event_type": "BUYBACK",
            "headline": "HDFC Bank announces ₹2,500 crore buyback at ₹1,800/share",
            "pdf_url": "https://example.com/hdfc-buyback.pdf",
            "source": "BSE",
            "filed_at": utcnow() - timedelta(days=1),
        },
        {
            "symbol": "WIPRO",
            "exchange": "NSE",
            "event_type": "QUARTERLY_RESULT",
            "headline": "Wipro Q4 FY26 Revenue misses estimates by 3%",
            "pdf_url": "https://example.com/wipro-q4.pdf",
            "source": "NSE",
            "filed_at": utcnow() - timedelta(days=2),
        },
    ]
    ann_map: dict[str, Announcement] = {}
    for a in db.query(Announcement).all():
        ann_map[a.headline] = a
    for ad in announcements_data:
        if ad["headline"] not in existing_announcements:
            a = Announcement(**ad)
            db.add(a)
            db.flush()
            ann_map[a.headline] = a

    # ── Analyses ──────────────────────────────────────────────────────────
    existing_analyses = {a.announcement_id for a in db.query(Analysis).all()}
    analyses_map: dict[int, Analysis] = {a.announcement_id: a for a in db.query(Analysis).all()}
    analyses_data = [
        {
            "headline": "Reliance Industries Q4 FY26 Net Profit up 18% YoY",
            "model": "deepseek-chat",
            "sentiment": "positive",
            "sentiment_score": 82.0,
            "confidence": 0.91,
            "recommendation": "BUY",
            "rationale": "Strong topline and margin expansion. ARPU growth in Jio segment.",
        },
        {
            "headline": "TCS declares ₹29/share final dividend for FY26",
            "model": "deepseek-chat",
            "sentiment": "positive",
            "sentiment_score": 60.0,
            "confidence": 0.78,
            "recommendation": "HOLD",
            "rationale": "Dividend positive but already priced in.",
            "dividend_per_share": 29.0,
        },
        {
            "headline": "Wipro Q4 FY26 Revenue misses estimates by 3%",
            "model": "deepseek-chat",
            "sentiment": "negative",
            "sentiment_score": -45.0,
            "confidence": 0.82,
            "recommendation": "SELL",
            "rationale": "Revenue miss signals weakness in discretionary IT spend.",
        },
    ]
    for ad in analyses_data:
        ann = ann_map.get(ad["headline"])
        if ann is None or ann.id in existing_analyses:
            continue
        kwargs = {k: v for k, v in ad.items() if k != "headline"}
        ana = Analysis(announcement_id=ann.id, **kwargs)
        db.add(ana)
        db.flush()
        analyses_map[ann.id] = ana

    # ── Signals ───────────────────────────────────────────────────────────
    existing_signals = {s.id for s in db.query(Signal).all()}
    signals_map: list[Signal] = []
    signals_data = [
        {
            "announcement_headline": "Reliance Industries Q4 FY26 Net Profit up 18% YoY",
            "symbol": "RELIANCE",
            "action": "BUY",
            "confidence": 0.91,
            "position_size_pct": 8.0,
            "rationale": "Confidence above threshold; positive sentiment.",
            "status": "placed",
        },
        {
            "announcement_headline": "Wipro Q4 FY26 Revenue misses estimates by 3%",
            "symbol": "WIPRO",
            "action": "SELL",
            "confidence": 0.82,
            "position_size_pct": 5.0,
            "rationale": "Revenue miss with negative sentiment.",
            "status": "filled",
        },
        {
            "announcement_headline": "TCS declares ₹29/share final dividend for FY26",
            "symbol": "TCS",
            "action": "HOLD",
            "confidence": 0.78,
            "rationale": "Dividend priced in; hold existing position.",
            "status": "pending",
        },
        {
            "announcement_headline": "HDFC Bank announces ₹2,500 crore buyback at ₹1,800/share",
            "symbol": "HDFC",
            "action": "BUY",
            "confidence": 0.69,
            "position_size_pct": 4.0,
            "rationale": "Buyback at premium provides price support.",
            "status": "pending",
        },
    ]
    for sd in signals_data:
        ann = ann_map.get(sd["announcement_headline"])
        if ann is None:
            continue
        ana = analyses_map.get(ann.id)
        # Don't double-insert — check if a signal for this symbol+status exists
        kwargs = {k: v for k, v in sd.items() if k != "announcement_headline"}
        sig = Signal(
            analysis_id=ana.id if ana else None,
            strategy_id=s_aggressive.id if s_aggressive else None,
            **kwargs,
        )
        db.add(sig)
        db.flush()
        signals_map.append(sig)

    # ── Positions ─────────────────────────────────────────────────────────
    existing_positions = {p.symbol for p in db.query(Position).all()}
    positions_data = [
        {
            "symbol": "RELIANCE",
            "quantity": 50,
            "average_price": 2850.0,
            "last_price": 2942.5,
            "unrealized_pnl": 4625.0,
        },
        {
            "symbol": "TCS",
            "quantity": 25,
            "average_price": 3520.0,
            "last_price": 3485.0,
            "unrealized_pnl": -875.0,
        },
    ]
    for pd_ in positions_data:
        if pd_["symbol"] not in existing_positions:
            db.add(Position(**pd_))

    # ── Prompt Templates ──────────────────────────────────────────────────
    SYSTEM_PROMPT = (
        "You are an expert financial analyst specializing in Indian equities. "
        "Analyze stock exchange announcements and return structured JSON with sentiment, "
        "recommendation, and rationale. Never make up data."
    )
    existing_prompt_types = {p.event_type for p in db.query(PromptTemplate).all()}
    prompt_types = [
        "QUARTERLY_RESULT", "DIVIDEND", "ACQUISITION", "BUYBACK", "BONUS",
        "RIGHTS_ISSUE", "BOARD_MEETING", "GENERAL_MEETING", "ORDER_WIN",
        "REGULATORY", "INSIDER_TRADING", "PROMOTER_CHANGE",
        "CORPORATE_ACTION", "MERGER", "DEFAULT", "ANNUAL_RESULT",
    ]
    user_tmpl = (
        "Analyze this exchange filing: {{pdf_url}}\n\n"
        "Return JSON with keys: sentiment, sentiment_score (-100..100), "
        "confidence (0..1), recommendation (buy/sell/hold), rationale."
    )
    for event_type in prompt_types:
        if event_type not in existing_prompt_types:
            t = PromptTemplate(
                event_type=event_type,
                system_prompt=SYSTEM_PROMPT,
                user_template=user_tmpl,
                model="deepseek-chat",
                temperature=0.2,
                max_tokens=2000,
                version=1,
                updated_by="seed",
            )
            db.add(t)
            db.flush()
            db.add(PromptHistory(
                template_id=t.id,
                version=1,
                system_prompt=SYSTEM_PROMPT,
                user_template=user_tmpl,
                model="deepseek-chat",
                temperature=0.2,
                max_tokens=2000,
                change_note="initial seed",
            ))

    # ── Backtest Run ──────────────────────────────────────────────────────
    existing_runs = db.query(BacktestRun).count()
    if existing_runs == 0:
        today = date.today()
        run = BacktestRun(
            name="Demo run — last 30 days",
            strategy_id=s_aggressive.id if s_aggressive else None,
            start_date=today - timedelta(days=30),
            end_date=today - timedelta(days=1),
            initial_capital=100_000.0,
            status="done",
            config={},
            results={
                "announcements_processed": 45,
                "signals_generated": 20,
                "blocked_trades": 3,
                "metrics": {
                    "total_return_pct": 4.32,
                    "sharpe": 1.28,
                    "max_drawdown_pct": -2.15,
                    "win_rate_pct": 58.3,
                    "profit_factor": 1.74,
                    "total_trades": 12,
                },
            },
            created_at=utcnow() - timedelta(days=1),
            finished_at=utcnow() - timedelta(hours=23),
        )
        db.add(run)

    db.commit()
    print("✓ Demo data seeded successfully.")


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
