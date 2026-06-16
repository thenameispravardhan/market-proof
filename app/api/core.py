"""`/api/core` — Query endpoints for core data tables, plus operator
triggers to (re)run the analyzer on stored announcements.

Endpoints:
  GET  /api/announcements/recent?limit=N
  GET  /api/analyses/recent?limit=N
  GET  /api/signals/recent?limit=N
  GET  /api/positions
  GET  /api/trades?limit=N
  GET  /api/dashboard/summary
  POST /api/analyses/run/{announcement_id}   # queue one announcement
  POST /api/analyses/backfill?limit=N        # queue N most recent unanalyzed
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.monitors.base import CHANNEL_NEW
from app.services.event_bus import event_bus

log = get_logger(__name__)

router = APIRouter(tags=["core"])
init_db()


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime as a UTC ISO-8601 string with a `Z` suffix.

    SQLAlchemy's default `DateTime` column strips timezone info on
    read, so `dt.isoformat()` returns a naive string like
    `2026-06-13T05:25:18` — and JavaScript's `new Date(...)` will
    then interpret that in the browser's local timezone, producing
    a 5h30m shift on Indian clients. By explicitly appending `Z`,
    we tell the consumer "this is UTC" so they can convert to
    IST (or anything else) correctly.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Naive datetime: we stored everything as UTC, so re-attach
        # the UTC tzinfo before formatting.
        dt = dt.replace(tzinfo=timezone.utc)
    # .isoformat() with +00:00 → swap to 'Z' for cleanliness.
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
        "filed_at": _iso_utc(a.filed_at),
        "received_at": _iso_utc(a.received_at),
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
        "created_at": _iso_utc(a.created_at),
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
        "created_at": _iso_utc(s.created_at),
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
        "opened_at": _iso_utc(p.opened_at),
        "updated_at": _iso_utc(p.updated_at),
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
        "executed_at": _iso_utc(t.executed_at),
        "created_at": _iso_utc(t.created_at),
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
    status: str | None = Query(
        None,
        description="Filter by trade status (e.g. 'filled'). Useful so a "
        "burst of rejected HOLD signals doesn't crowd real fills out of "
        "the newest-N window.",
    ),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(Trade).order_by(Trade.created_at.desc())
    if status:
        stmt = stmt.where(Trade.status == status)
    rows = db.execute(stmt.limit(limit)).scalars().all()
    return [_ser_trade(t) for t in rows]


def _publish_payload(a: Announcement) -> dict[str, Any]:
    """Build the same `announcements.new` payload the monitors publish,
    so a re-queued announcement flows through the identical pipeline."""
    return {
        "announcement_id": a.id,
        "exchange": a.exchange,
        "company": a.symbol,
        "title": a.headline,
        "pdf_url": a.pdf_url,
        "posted_at": a.filed_at.isoformat() if a.filed_at else None,
    }


@router.post("/api/analyses/run/{announcement_id}")
async def run_analysis(
    announcement_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Queue one stored announcement for analysis.

    Publishes `announcements.new` on the in-process event bus; the
    running analyzer picks it up exactly like a fresh scrape (DeepSeek
    call → rules engine → risk engine → execution). The analyzer skips
    announcements that already have an `analyses` row, so re-running
    is safe and free.
    """
    a = db.get(Announcement, announcement_id)
    if a is None:
        raise HTTPException(404, f"announcement {announcement_id} not found")
    already = db.execute(
        select(func.count()).select_from(Analysis).where(
            Analysis.announcement_id == announcement_id
        )
    ).scalar_one() > 0
    await event_bus.publish(CHANNEL_NEW, _publish_payload(a))
    log.info(
        "analyses.run_queued",
        announcement_id=announcement_id,
        already_analyzed=already,
    )
    return {
        "status": "queued",
        "announcement_id": announcement_id,
        "already_analyzed": already,
    }


@router.post("/api/analyses/backfill")
async def backfill_analyses(
    limit: int = Query(5, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Queue the N most recent announcements that have no analysis yet.

    Each queued announcement costs one DeepSeek call, so the limit is
    capped at 50 per request.
    """
    analyzed = select(Analysis.announcement_id)
    rows = db.execute(
        select(Announcement)
        .where(Announcement.id.not_in(analyzed))
        .order_by(Announcement.received_at.desc())
        .limit(limit)
    ).scalars().all()
    for a in rows:
        await event_bus.publish(CHANNEL_NEW, _publish_payload(a))
    ids = [a.id for a in rows]
    log.info("analyses.backfill_queued", count=len(ids), announcement_ids=ids)
    return {"status": "queued", "count": len(ids), "announcement_ids": ids}


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
