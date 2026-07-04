"""Deterministic fast track: headline → signal with no LLM call.

Phase 2 of the roadmap (plan.txt). The news edge decays in seconds, so
for a handful of HIGH-CONVICTION, unambiguous event shapes we parse the
exchange headline deterministically and go straight to the rules engine
— a 1-2s path instead of the multi-second LLM track. Everything this
module does not confidently match falls through to the LLM track
untouched (return None = "not my trade").

Gated by the `FAST_TRACK_ENABLED` Settings toggle, default OFF — the
legacy LLM-for-everything behavior stays the default.

Deliberately TIGHT coverage (same philosophy as the noise filter's
denylist): a wrong fast-track BUY costs real money, a missed fast-track
just means the LLM decides a few seconds later. Current patterns:

  1. ORDER_WIN with an explicit INR value — "bags order worth Rs 450
     crore". Needs BOTH an order-context keyword AND a parseable value;
     confidence scales with the value.
  2. KMP resignation — MD / CEO / CFO / whole-time director / chairman
     resigning. Routine succession ("resignation and appointment") and
     independent-director exits are left to the LLM.
  3. BUYBACK with an explicit INR value.

Known blind spot (documented, accepted for v1): the deal value is not
scaled against the company's market cap — a Rs 100 crore order is
transformative for a small-cap and noise for RELIANCE. The operator's
rules (deal_value_inr_crore thresholds, symbol/sector gates) are the
control until a market-cap feed lands. The LLM track has the same
blindness today.

Everything returns a fully-validated `AnalysisResponse`, so the fast
track feeds the SAME rules engine, risk checks, and signal path as the
LLM — only the analysis producer differs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.analyzer.schemas import AnalysisResponse
from app.logging_config import get_logger

log = get_logger(__name__)


# -- INR value parsing -----------------------------------------------------

# "Rs. 1,234.56 crore", "₹450 cr", "INR 89.5 crores", "Rs 20 lakh",
# "Rs 1.2 billion". INR only — USD/EUR orders need an FX judgement call,
# which is exactly what the LLM track is for.
_INR_VALUE_RE = re.compile(
    r"(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)\s*"
    r"(crores?|cr\b|lakhs?|lacs?|million|mn\b|billion|bn\b)",
    re.IGNORECASE,
)

_UNIT_TO_CRORE = {
    "crore": 1.0,
    "crores": 1.0,
    "cr": 1.0,
    "lakh": 0.01,
    "lakhs": 0.01,
    "lac": 0.01,
    "lacs": 0.01,
    "million": 0.1,   # 1 million INR = 0.1 crore
    "mn": 0.1,
    "billion": 100.0,
    "bn": 100.0,
}


def parse_inr_crore(text: str) -> Optional[float]:
    """Extract the largest INR amount in the text, normalised to crore.

    Largest wins because filings often mention component values next to
    the total ("orders of Rs 120 crore and Rs 330 crore, aggregating to
    Rs 450 crore").
    """
    best: Optional[float] = None
    for m in _INR_VALUE_RE.finditer(text):
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = m.group(2).lower().rstrip(".")
        value = amount * _UNIT_TO_CRORE.get(unit, 0.0)
        if value <= 0:
            continue
        if best is None or value > best:
            best = value
    return best


# -- Pattern vocabulary ------------------------------------------------------

# Word-boundary regex, not substring: short tokens like "won" / "loa"
# would otherwise match inside unrelated words ("wonder", "upload").
_ORDER_CONTEXT_RE = re.compile(
    r"\b(?:orders?|contracts?|work order|purchase order|"
    r"letter of award|letter of intent|loa|loi|"
    r"bags?|bagged|secures?|secured|wins?|won|awarded)\b",
    re.IGNORECASE,
)

# Any of these anywhere in the headline kills the order-win fast track —
# the "order" is going away, not arriving, or is still hypothetical.
_ORDER_NEGATIVE_GUARDS = (
    "cancel", "terminat", "withdraw", "suspend", "dispute",
    "bid", "bidding", "tender submitted", "participat",
)

# Senior-management roles whose exit is a tradeable negative.
_KMP_ROLES = (
    "managing director", "chief executive", "ceo",
    "chief financial officer", "cfo", "whole-time director",
    "whole time director", "chairman",
)

# Resignations the fast track leaves to the LLM: independent /
# non-executive exits are common governance churn, and a combined
# "resignation and appointment" is routine succession.
_RESIGNATION_SKIP_GUARDS = (
    "independent director", "non-executive", "non executive",
    "appointment", "appointed", "re-appoint", "reappoint",
)

_BUYBACK_CONTEXT = ("buyback", "buy-back")
_BUYBACK_APPROVAL = ("approve", "consider", "board meeting")


# -- Result type -------------------------------------------------------------


@dataclass(frozen=True)
class FastTrackMatch:
    """A deterministic parse the analyzer can act on without the LLM."""

    pattern: str                  # machine tag, e.g. "order_win_value"
    response: AnalysisResponse    # same shape the LLM track produces


# Stand-in for DeepSeekResult so `_persist_analysis_and_signal` (which
# only reads these attributes) works unchanged on the fast track.
@dataclass(frozen=True)
class FastTrackProducer:
    latency_ms: float
    model: str = "fast-track"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


# -- Individual parsers -------------------------------------------------------


def _match_order_win(headline_lc: str, headline: str) -> Optional[FastTrackMatch]:
    if not _ORDER_CONTEXT_RE.search(headline_lc):
        return None
    if any(kw in headline_lc for kw in _ORDER_NEGATIVE_GUARDS):
        return None
    value = parse_inr_crore(headline)
    if value is None or value < 25.0:
        # No value / too small to be an unambiguous catalyst → LLM track.
        return None
    if value >= 500.0:
        confidence, score = 0.88, 80.0
    elif value >= 100.0:
        confidence, score = 0.80, 70.0
    else:
        confidence, score = 0.72, 60.0
    response = AnalysisResponse(
        event_type="ORDER_WIN",
        summary=f"Order win with explicit value of Rs {value:g} crore in the exchange headline.",
        sentiment="positive",
        sentiment_score=score,
        confidence=confidence,
        recommendation="BUY",
        reasoning=(
            f"Deterministic fast track: headline contains an order-win "
            f"context and an explicit value of Rs {value:g} crore. "
            f'Headline: "{headline.strip()}"'
        ),
        key_numbers={"deal_value_inr_crore": value},
    )
    return FastTrackMatch(pattern="order_win_value", response=response)


def _match_kmp_resignation(headline_lc: str, headline: str) -> Optional[FastTrackMatch]:
    if "resign" not in headline_lc:
        return None
    if any(kw in headline_lc for kw in _RESIGNATION_SKIP_GUARDS):
        return None
    role = next((r for r in _KMP_ROLES if r in headline_lc), None)
    if role is None:
        return None
    response = AnalysisResponse(
        event_type="OTHER",
        summary=f"Resignation of a key managerial person ({role}) per the exchange headline.",
        sentiment="negative",
        sentiment_score=-65.0,
        confidence=0.75,
        recommendation="SELL",
        reasoning=(
            f"Deterministic fast track: headline reports the resignation "
            f"of the {role} with no accompanying appointment. "
            f'Headline: "{headline.strip()}"'
        ),
    )
    return FastTrackMatch(pattern="kmp_resignation", response=response)


def _match_buyback(headline_lc: str, headline: str) -> Optional[FastTrackMatch]:
    if not any(kw in headline_lc for kw in _BUYBACK_CONTEXT):
        return None
    if not any(kw in headline_lc for kw in _BUYBACK_APPROVAL):
        return None
    value = parse_inr_crore(headline)
    if value is None or value < 25.0:
        return None
    response = AnalysisResponse(
        event_type="BUYBACK",
        summary=f"Buyback of Rs {value:g} crore per the exchange headline.",
        sentiment="positive",
        sentiment_score=65.0,
        confidence=0.78,
        recommendation="BUY",
        reasoning=(
            f"Deterministic fast track: headline reports a buyback with an "
            f"explicit value of Rs {value:g} crore. "
            f'Headline: "{headline.strip()}"'
        ),
        key_numbers={"buyback_value_inr_crore": value},
    )
    return FastTrackMatch(pattern="buyback_value", response=response)


# Order matters: the most specific / highest-conviction parser first.
_PARSERS = (_match_order_win, _match_buyback, _match_kmp_resignation)


def evaluate_fast_track(headline: str) -> Optional[FastTrackMatch]:
    """Try every fast-track parser against the headline.

    Returns the first match, or None → the announcement takes the
    normal LLM track. Never raises: a parser crash is logged and treated
    as no-match (fail back to the LLM, never fail the pipeline).
    """
    if not headline or not headline.strip():
        return None
    headline_lc = headline.lower()
    for parser in _PARSERS:
        try:
            match = parser(headline_lc, headline)
        except Exception:  # noqa: BLE001
            log.exception("fast_track.parser_crashed", parser=parser.__name__)
            continue
        if match is not None:
            return match
    return None
