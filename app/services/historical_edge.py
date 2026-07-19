"""Edge Memory — the bot's self-learning conviction engine.

The one advantage a $12 server has over a colocated HFT machine: it can
remember. Every signal's real outcome (price +5m / +30m) is logged in
`signal_outcomes`. This module reads that private track record back and,
for a NEW signal, answers the only question that matters:

    "When I've taken signals LIKE THIS before, what actually happened?"

HFT firms can't do this in the small/mid-cap news universe — they don't
trade there, so they have no such data. It is the one edge that compounds:
every real trading day makes this sharper, and it can never be bought.

Design:
  - PURE + READ-ONLY. No side effects, never raises into the caller.
  - Direction-adjusted: `signal_outcomes.move_30m_pct` is the RAW price
    change; for a BUY a rise is favorable, for a SELL a fall is. We flip
    the sign for SELL so "favorable move" and "win" mean the same thing
    regardless of side.
  - Tiered evidence, most-specific first:
      1. this exact symbol + action  (n >= MIN_SYMBOL)  basis="symbol"
      2. this action, any symbol      (n >= MIN_ACTION)  basis="action"
      3. not enough evidence -> None
  - FAIL-OPEN: thin/absent history returns None (or verdict
    "insufficient"), so a cold-start bot still trades. The edge can veto
    a well-evidenced loser, never block everything on a hunch.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SignalOutcome
from app.logging_config import get_logger

log = get_logger(__name__)

# Minimum samples before a tier is trustworthy. Per-symbol needs fewer
# (it's more specific); the action-wide pool needs more to matter.
MIN_SYMBOL = 5
MIN_ACTION = 20
# How many recent complete outcomes to scan (newest first). Real signals
# accumulate slowly, so a wide window is fine and keeps early data usable.
LOOKBACK = 2000


@dataclass(frozen=True)
class EdgeStats:
    basis: str            # "symbol" | "action"
    symbol: Optional[str]  # set when basis == "symbol"
    action: str
    n: int
    win_rate: float       # fraction of samples with a favorable 30m move
    avg_move_5m: float    # mean favorable move at +5m (%)
    expectancy: float     # mean favorable move at +30m (%) — the headline
    best: float
    worst: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _favorable(move_pct: Optional[float], action: str) -> Optional[float]:
    """Raw % move -> favorable move for the trade's side."""
    if move_pct is None:
        return None
    return float(move_pct) if action == "BUY" else -float(move_pct)


def _summarise(rows: list[SignalOutcome], action: str, *, basis: str,
               symbol: Optional[str]) -> Optional[EdgeStats]:
    favs_30 = [_favorable(r.move_30m_pct, action) for r in rows]
    favs_30 = [f for f in favs_30 if f is not None]
    if not favs_30:
        return None
    favs_5 = [_favorable(r.move_5m_pct, action) for r in rows]
    favs_5 = [f for f in favs_5 if f is not None]
    n = len(favs_30)
    wins = sum(1 for f in favs_30 if f > 0)
    return EdgeStats(
        basis=basis,
        symbol=symbol,
        action=action,
        n=n,
        win_rate=round(wins / n, 4),
        avg_move_5m=round(sum(favs_5) / len(favs_5), 4) if favs_5 else 0.0,
        expectancy=round(sum(favs_30) / n, 4),
        best=round(max(favs_30), 4),
        worst=round(min(favs_30), 4),
    )


def compute_edge(
    session: Session,
    *,
    symbol: str,
    action: str,
    min_symbol: int = MIN_SYMBOL,
    min_action: int = MIN_ACTION,
    lookback: int = LOOKBACK,
) -> Optional[EdgeStats]:
    """The track record for a signal like this one, or None if too thin.

    Only BUY / SELL have a direction and thus an edge; HOLD/BLOCK return
    None. Never raises — any failure degrades to None (fail-open)."""
    action = (action or "").upper()
    if action not in ("BUY", "SELL"):
        return None
    try:
        # Pull the recent complete outcomes for this action once, then
        # split into the per-symbol subset in memory (cheap, one query).
        rows = list(
            session.execute(
                select(SignalOutcome)
                .where(
                    SignalOutcome.action == action,
                    SignalOutcome.move_30m_pct.is_not(None),
                    SignalOutcome.price_at_signal.is_not(None),
                    SignalOutcome.price_at_signal > 0,
                )
                .order_by(SignalOutcome.id.desc())
                .limit(lookback)
            ).scalars()
        )
        if not rows:
            return None
        sym = (symbol or "").upper()
        sym_rows = [r for r in rows if (r.symbol or "").upper() == sym]
        if len(sym_rows) >= min_symbol:
            return _summarise(sym_rows, action, basis="symbol", symbol=sym)
        if len(rows) >= min_action:
            return _summarise(rows, action, basis="action", symbol=None)
        return None
    except Exception:  # noqa: BLE001 — advisory analytics, never break a trade
        log.exception("historical_edge.compute_failed", symbol=symbol, action=action)
        return None


def edge_verdict(
    edge: Optional[EdgeStats],
    *,
    min_n: int,
    min_expectancy_pct: float,
) -> tuple[str, str]:
    """Turn an EdgeStats into an allow/block decision for the optional
    risk gate. FAIL-OPEN: thin evidence never blocks.

    Returns (verdict, reason) where verdict is one of:
      "allow"        — evidence supports the trade (or the gate abstains)
      "block"        — enough evidence AND the cohort loses money
      "insufficient" — not enough evidence to judge (treated as allow)
    """
    if edge is None or edge.n < max(1, int(min_n)):
        return ("insufficient", "not enough history to judge — allowed")
    if edge.expectancy < float(min_expectancy_pct):
        return (
            "block",
            f"{edge.basis} history over {edge.n} signals averaged "
            f"{edge.expectancy:+.2f}% (< {min_expectancy_pct:+.2f}% floor)",
        )
    return (
        "allow",
        f"{edge.basis} history over {edge.n} signals averaged "
        f"{edge.expectancy:+.2f}% (win rate {edge.win_rate * 100:.0f}%)",
    )


def book_expectancy(session: Session, *, lookback: int = LOOKBACK) -> dict[str, Any]:
    """Overall track record by action — the whole book at a glance, for
    the dashboard panel. Read-only, never raises."""
    out: dict[str, Any] = {}
    try:
        for action in ("BUY", "SELL"):
            rows = list(
                session.execute(
                    select(SignalOutcome)
                    .where(
                        SignalOutcome.action == action,
                        SignalOutcome.move_30m_pct.is_not(None),
                        SignalOutcome.price_at_signal.is_not(None),
                        SignalOutcome.price_at_signal > 0,
                    )
                    .order_by(SignalOutcome.id.desc())
                    .limit(lookback)
                ).scalars()
            )
            stats = _summarise(rows, action, basis="action", symbol=None)
            out[action] = stats.to_dict() if stats else None
    except Exception:  # noqa: BLE001
        log.exception("historical_edge.book_expectancy_failed")
    return out
