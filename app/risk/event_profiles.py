"""Event-type trade-management matrix.

A buyback and an order win are not the same trade, yet the bot used to
give every announcement the same flat stop, target, and hold window.
This module attaches a per-event-type *profile* that tunes:

  - the ATR stop multiplier (`sl_atr_mult`) and the %-stop fallback
    (`sl_default_pct`) — how much room the initial stop gets,
  - the reward:risk multiple (`target_rr`) — how far the target sits,
  - the maximum hold window (`max_hold_seconds`) — news alpha decays at
    different rates for different events,
  - a per-event confidence floor (`min_confidence`) — priced-in events
    (dividends, splits) need stronger conviction to be worth trading.

It deliberately does **not** touch position size: sizing stays risk-math
driven and decoupled from the LLM's conviction (an explicit product
decision). The profile shapes the *trade management*, not the *bet size*.

Defaults are research-informed starting points, not gospel — every field
is `None`-able and falls back to the corresponding global `Settings`
value via `resolved()`, so an operator can still tune globally and only
the deliberate per-event deviations stick.

Rough taxonomy:
  - Discrete high-impact catalysts (ORDER_WIN, ACQUISITION, MERGER):
    let winners run — wider target RR, longer hold.
  - Earnings (Q*_RESULTS, ANNUAL_RESULTS): two-sided and whippy — a
    slightly tighter stop to cut whipsaw, standard RR.
  - Largely priced-in / cosmetic (DIVIDEND, BONUS, STOCK_SPLIT,
    RIGHTS_ISSUE, BUYBACK): lower edge — tighter stop, shorter hold,
    higher confidence bar.
  - Ambiguous (BOARD_MEETING, OTHER): conservative.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(frozen=True)
class ResolvedProfile:
    """A profile with every field filled in (no None) against Settings."""

    sl_atr_mult: float
    sl_default_pct: float
    target_rr: float
    max_hold_seconds: int
    min_confidence: float


@dataclass(frozen=True)
class EventProfile:
    """Per-event overrides. `None` means "use the global Settings default"."""

    sl_atr_mult: Optional[float] = None
    sl_default_pct: Optional[float] = None
    target_rr: Optional[float] = None
    max_hold_seconds: Optional[int] = None
    min_confidence: Optional[float] = None

    def resolved(self, settings: "Settings") -> ResolvedProfile:
        """Fill every None from the global Settings so the caller gets
        concrete numbers. The per-event `min_confidence` never lowers the
        global floor — it can only raise the bar (max of the two)."""
        global_floor = float(getattr(settings, "MIN_SENTIMENT_CONFIDENCE", 0.0) or 0.0)
        ev_floor = self.min_confidence if self.min_confidence is not None else global_floor
        return ResolvedProfile(
            sl_atr_mult=float(
                self.sl_atr_mult
                if self.sl_atr_mult is not None
                else settings.ATR_STOP_MULT
            ),
            sl_default_pct=float(
                self.sl_default_pct
                if self.sl_default_pct is not None
                else settings.DEFAULT_SL_PCT
            ),
            target_rr=float(
                self.target_rr
                if self.target_rr is not None
                else settings.DEFAULT_TARGET_RR
            ),
            max_hold_seconds=int(
                self.max_hold_seconds
                if self.max_hold_seconds is not None
                else settings.MAX_HOLD_SECONDS
            ),
            min_confidence=max(global_floor, float(ev_floor)),
        )


# Keyed by EventType.value (see app/analyzer/schemas.py). DEFAULT (and any
# event not listed) falls through to the all-None profile = pure Settings.
DEFAULT_PROFILE = EventProfile()

EVENT_PROFILES: dict[str, EventProfile] = {
    # --- Discrete high-impact catalysts: let winners run -----------------
    "ORDER_WIN": EventProfile(sl_atr_mult=2.0, target_rr=3.5, max_hold_seconds=1500, min_confidence=0.70),
    "ACQUISITION": EventProfile(sl_atr_mult=2.0, target_rr=3.5, max_hold_seconds=1500, min_confidence=0.70),
    "MERGER": EventProfile(sl_atr_mult=2.0, target_rr=3.0, max_hold_seconds=1500, min_confidence=0.70),
    # --- Earnings: two-sided / whippy — tighter stop, standard RR --------
    "Q1_RESULTS": EventProfile(sl_atr_mult=1.8, target_rr=3.0, max_hold_seconds=900, min_confidence=0.70),
    "Q2_RESULTS": EventProfile(sl_atr_mult=1.8, target_rr=3.0, max_hold_seconds=900, min_confidence=0.70),
    "Q3_RESULTS": EventProfile(sl_atr_mult=1.8, target_rr=3.0, max_hold_seconds=900, min_confidence=0.70),
    "Q4_RESULTS": EventProfile(sl_atr_mult=1.8, target_rr=3.0, max_hold_seconds=900, min_confidence=0.70),
    "ANNUAL_RESULTS": EventProfile(sl_atr_mult=1.8, target_rr=3.0, max_hold_seconds=900, min_confidence=0.70),
    # --- Mostly priced-in / cosmetic: lower edge — tighter, shorter ------
    "BUYBACK": EventProfile(sl_atr_mult=1.8, sl_default_pct=4.0, target_rr=2.5, max_hold_seconds=900, min_confidence=0.72),
    "DIVIDEND": EventProfile(sl_atr_mult=1.5, sl_default_pct=3.0, target_rr=2.0, max_hold_seconds=600, min_confidence=0.75),
    "BONUS": EventProfile(sl_atr_mult=1.5, sl_default_pct=3.0, target_rr=2.0, max_hold_seconds=600, min_confidence=0.78),
    "STOCK_SPLIT": EventProfile(sl_atr_mult=1.5, sl_default_pct=3.0, target_rr=2.0, max_hold_seconds=600, min_confidence=0.78),
    "RIGHTS_ISSUE": EventProfile(sl_atr_mult=1.8, sl_default_pct=4.0, target_rr=2.0, max_hold_seconds=720, min_confidence=0.75),
    # --- Ambiguous: conservative -----------------------------------------
    "BOARD_MEETING": EventProfile(sl_atr_mult=1.8, target_rr=2.0, max_hold_seconds=600, min_confidence=0.75),
    "OTHER": EventProfile(sl_atr_mult=2.0, target_rr=2.5, min_confidence=0.70),
}


def profile_for(event_type: Optional[str]) -> EventProfile:
    """Return the profile for an event type (DEFAULT for None/unknown).

    Case-insensitive; tolerates the analyzer's enum value or a raw string.
    """
    if not event_type:
        return DEFAULT_PROFILE
    return EVENT_PROFILES.get(str(event_type).strip().upper(), DEFAULT_PROFILE)
