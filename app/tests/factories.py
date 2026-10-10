"""Row factories shared by the evaluation-layer tests.

One call seeds the whole Announcement → Analysis → Signal → SignalOutcome
chain the measurement endpoints join over, so each test states only the
fields it is about.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db.models import Analysis, Announcement, Signal, SignalOutcome

_counter = {"n": 0}


def _utc_naive(dt: Optional[datetime]) -> datetime:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def seed_signal_chain(
    db: Session,
    *,
    symbol: str = "ACME",
    event_type: str = "ORDER_WIN",
    action: str = "BUY",
    status: str = "blocked",
    confidence: float = 0.8,
    sentiment: str = "positive",
    recommendation: Optional[str] = None,
    price: Optional[float] = 100.0,
    move_5m: Optional[float] = None,
    move_30m: Optional[float] = None,
    rationale: str = "test",
    created_at: Optional[datetime] = None,
    model: str = "deepseek-chat",
    headline: Optional[str] = None,
    with_outcome: bool = True,
) -> dict[str, Any]:
    _counter["n"] += 1
    when = _utc_naive(created_at)
    ann = Announcement(
        symbol=symbol, exchange="NSE", event_type=event_type,
        headline=headline or f"{symbol} {event_type} #{_counter['n']}",
        content_hash=f"factory-{_counter['n']}-{when.timestamp()}",
        filed_at=when - timedelta(seconds=20), received_at=when - timedelta(seconds=10),
    )
    db.add(ann)
    db.flush()
    an = Analysis(
        announcement_id=ann.id, model=model, sentiment=sentiment,
        sentiment_score=60.0 if sentiment == "positive" else -60.0,
        confidence=confidence,
        recommendation=recommendation or ("buy" if action == "BUY" else "sell" if action == "SELL" else "hold"),
        rationale=rationale, created_at=when,
    )
    db.add(an)
    db.flush()
    sig = Signal(
        analysis_id=an.id, symbol=symbol, action=action, confidence=confidence,
        rationale=rationale, status=status, created_at=when,
    )
    db.add(sig)
    db.flush()
    out = None
    if with_outcome:
        out = SignalOutcome(
            signal_id=sig.id, symbol=symbol, action=action, signal_status=status,
            price_at_signal=price,
            price_5m=None if (price is None or move_5m is None) else price * (1 + move_5m / 100),
            price_30m=None if (price is None or move_30m is None) else price * (1 + move_30m / 100),
            move_5m_pct=move_5m, move_30m_pct=move_30m, note="ok", created_at=when,
        )
        db.add(out)
    db.commit()
    return {"announcement": ann, "analysis": an, "signal": sig, "outcome": out}
