"""`/api/core` — Read-only query endpoints for core data tables.

Endpoints:
  GET /api/announcements/recent?limit=N
  GET /api/analyses/recent?limit=N
  GET /api/signals/recent?limit=N
  GET /api/positions
  GET /api/trades?limit=N
  GET /api/dashboard/summary
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.init import init_db
from app.db.models import (
    Analysis,
    Announcement,
    Position,
    Signal,
    Trade,
)
from app.db.session import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["core"])
init_db()


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ser_announcement(a: Announcement) -> dict[str, Any]:
    return {
        "id": a.id,
        "symbol": a.symbol,
        "exchange": a.exchange,
        "event_type": a.event_type,
        "headline": a.headline,
        "body": a.body,
        "pdf_url": a.pdf_url,
        "source": a.source,
        "filed_at": a.filed_at.isoformat() if a.filed_at else None,
        "received_at": a.received_at.isoformat() if a.received_at else None,
    }


def _ser_analysis(a: Analysis) -> dict[str, Any]:
    return {
        "id": a.id,
        "announcement_id": a.announcement_id,
        "model": a.model,
        "sentiment": a.sentiment,
        "sentiment_score": a.sentiment_score,
        "confidence": a.confidence,
        "recommendation": a.recommendation,
        "rationale": a.rationale,
        "deal_value_inr_crore": a.deal_value_inr_crore,
        "stake_change_pct": a.stake_change_pct,
        "dividend_per_share": a.dividend_per_share,
        "buyback_value_inr_crore": a.buyback_value_inr_crore,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _ser_signal(s: Signal) -> dict[str, Any]:
    return {
        "id": s.id,
        "analysis_id": s.analysis_id,
        "strategy_id": s.strategy_id,
        "rule_id": s.rule_id,
        "symbol": s.symbol,
        "action": s.action,
        "confidence": s.confidence,
        "position_size_pct": s.position_size_pct,
        "rationale": s.rationale,
        "status": s.status,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _ser_position(p: Position) -> dict[str, Any]:
    return {
        "id": p.id,
        "symbol": p.symbol,
        "quantity": p.quantity,
        "average_price": p.average_price,
        "last_price": p.last_price,
        "unrealized_pnl": p.unrealized_pnl,
        "strategy_id": p.strategy_id,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _ser_trade(t: Trade) -> dict[str, Any]:
    return {
        "id": t.id,
        "signal_id": t.signal_id,
        "broker_account_id": t.broker_account_id,
        "symbol": t.symbol,
        "side": t.side,
        "quantity": t.quantity,
        "price": t.price,
        "order_type": t.order_type,
        "status": t.status,
        "broker_order_id": t.broker_order_id,
        "pnl": t.pnl,
        "executed_at": t.executed_at.isoformat() if t.executed_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


# -------------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------------


@router.get("/api/announcements/recent")
def recent_announcements(
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Announcement).order_by(Announcement.received_at.desc()).limit(limit)
    ).scalars().all()
    return [_ser_announcement(a) for a in rows]


@router.get("/api/analyses/recent")
def recent_analyses(
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Analysis).order_by(Analysis.created_at.desc()).limit(limit)
    ).scalars().all()
    return [_ser_analysis(a) for a in rows]


@router.get("/api/signals/recent")
def recent_signals(
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Signal).order_by(Signal.created_at.desc()).limit(limit)
    ).scalars().all()
    return [_ser_signal(s) for s in rows]


@router.get("/api/positions")
def list_positions(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Position).order_by(Position.symbol.asc())
    ).scalars().all()
    return [_ser_position(p) for p in rows]


@router.get("/api/trades")
def list_trades(
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Trade).order_by(Trade.created_at.desc()).limit(limit)
    ).scalars().all()
    return [_ser_trade(t) for t in rows]


@router.get("/api/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Returns key metrics for the dashboard overview."""
    today = _utcnow().date()
    today_start = datetime(today.year, today.month, today.day)
    today_end = today_start + timedelta(days=1)

    # Open positions
    open_positions = db.execute(
        select(func.count()).select_from(Position)
    ).scalar_one_or_none() or 0

    # Today's realised P&L
    today_pnl_row = db.execute(
        select(func.coalesce(func.sum(Trade.pnl), 0)).where(
            Trade.executed_at >= today_start,
            Trade.executed_at < today_end,
            Trade.pnl.isnot(None),
        )
    ).scalar_one_or_none() or 0.0

    # Today's unrealised P&L
    today_unrealised = db.execute(
        select(func.coalesce(func.sum(Position.unrealized_pnl), 0)).where(
            Position.unrealized_pnl.isnot(None)
        )
    ).scalar_one_or_none() or 0.0

    # P&L series — last 14 trading days
    trades = db.execute(
        select(Trade)
        .where(
            Trade.executed_at >= datetime.combine(today - timedelta(days=14), datetime.min.time()),
            Trade.pnl.isnot(None),
        )
        .order_by(Trade.executed_at.asc())
    ).scalars().all()

    # Bucket by date
    from collections import defaultdict
    buckets: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.executed_at:
            d = t.executed_at.date().isoformat()
            buckets[d] += t.pnl or 0.0

    cum = 0.0
    pnl_series = []
    for i in range(14):
        d = (today - timedelta(days=13 - i)).isoformat()
        realized = buckets.get(d, 0.0)
        cum += realized
        pnl_series.append({"date": d, "realized": round(realized, 2), "cumulative": round(cum, 2)})

    return {
        "open_positions": int(open_positions),
        "hard_rules_count": 10,  # configurable limit, exposed for UI
        "todays_realized_pnl": round(float(today_pnl_row), 2),
        "todays_unrealized_pnl": round(float(today_unrealised), 2),
        "pnl_series": pnl_series,
    }
