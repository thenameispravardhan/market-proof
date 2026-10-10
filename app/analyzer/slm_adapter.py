"""Bridge between `tradebot-slm-v1` and the bot's analysis contract.

The fine-tuned model was NOT trained to emit `AnalysisResponse`. It was trained
on its own schema, supervised by measured market reaction:

    {"event_type": "ORDER_WIN", "materiality": "HIGH", "surprise": "MEDIUM",
     "facts": {"amount_inr_cr": 412.0, "amount_basis": "NEW_ORDER",
               "amount_to_mcap": 0.041},
     "direction": "UP", "mover": true, "shape": "IMMEDIATE",
     "price_path": [3, 8, 14, ...], "volume_path": [9.1, 6.4, ...]}

So swapping the endpoint is not enough: the prompt has to match the training
format (otherwise the model is off-distribution and the fine-tune is wasted),
and the reply has to be mapped back onto `AnalysisResponse` (otherwise every
filing fails schema validation).

Both directions live here so the analyzer keeps one code path and the mapping
stays auditable in one file. The raw model JSON is preserved and stored, so a
mapping decision can always be re-derived from what the model actually said.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.analyzer.prompts import detect_event_type

_IST = timezone(timedelta(hours=5, minutes=30))

# The system prompt the model saw for all 146,500 training examples. Changing
# it moves the model off-distribution — treat it as part of the weights.
SYSTEM_PROMPT = (
    "You are a financial analyst specialised in Indian NSE/BSE corporate "
    "filings. Read the filing, extract the material facts, and reason about "
    "the likely market reaction. Return only the requested JSON. Never use "
    "information from after the filing timestamp."
)

# The model's event vocabulary -> the bot's `EventType`. The model's taxonomy
# is coarser in places (one RESULTS bucket for four quarters) and finer in
# others (INVESTOR_MEET, RATING, TRADING_WINDOW have no bot equivalent and
# correctly collapse to OTHER — they are the administrative noise the pre-LLM
# filter already drops).
_EVENT_MAP: dict[str, str] = {
    "ORDER_WIN": "ORDER_WIN",
    "MA": "ACQUISITION",
    "DIVIDEND": "DIVIDEND",
    "BUYBACK": "BUYBACK",
    "BOARD_MEETING": "BOARD_MEETING",
    "INVESTOR_MEET": "OTHER",
    "PRESS": "OTHER",
    "REGULATORY": "OTHER",
    "MGMT_CHANGE": "OTHER",
    "FUNDRAISE": "OTHER",
    "RATING": "OTHER",
    "SHAREHOLDER": "OTHER",
    "TRADING_WINDOW": "OTHER",
    "OTHER": "OTHER",
    # RESULTS is handled separately — see `_map_event_type`.
}

_RESULTS_TYPES = frozenset(
    {"Q1_RESULTS", "Q2_RESULTS", "Q3_RESULTS", "Q4_RESULTS", "ANNUAL_RESULTS"}
)

# `price_path` is the market-adjusted return in TENTHS OF A PERCENT at
# t1..t15 | t30 | t60. Index 14 is t15 — the horizon every outcome label in
# this project is measured at, so it is the one the score should reflect.
_T15 = 14
# ±3.0% (i.e. ±30 tenths) saturates the -100..100 sentiment scale. Chosen to
# match the ±1/2/3% thresholds the baselines are reported at, not tuned.
_SCORE_SATURATION_TENTHS = 30.0


# Line labels of the MARKET CONTEXT block, in training order. The training
# script (train_sft.py, kept outside git) is the authority on these strings:
# if a label differs there, change it HERE and only here. A field the bot
# cannot measure live is omitted, never filled with a placeholder.
MARKET_CONTEXT_KEYS: dict[str, str] = {
    "last_trade": "last_trade",
    "volume_ratio": "pre_news_volume_ratio",
    "cap_tier": "cap_tier",
    "session": "session",
}


def session_of(filed_at_utc: Optional[datetime]) -> Optional[str]:
    """The training corpus' session label for a filing time:
    same_session (09:15-15:30 IST on a weekday), same_day_preopen (before
    09:15 on a weekday) or next_session (after the close / weekend)."""
    if filed_at_utc is None:
        return None
    aware = filed_at_utc if filed_at_utc.tzinfo else filed_at_utc.replace(tzinfo=timezone.utc)
    ist = aware.astimezone(_IST)
    if ist.weekday() >= 5:
        return "next_session"
    hm = ist.hour * 60 + ist.minute
    if hm < 9 * 60 + 15:
        return "same_day_preopen"
    if hm < 15 * 60 + 30:
        return "same_session"
    return "next_session"


def market_context_for(symbol: str, filed_at: Optional[datetime]) -> dict[str, Any]:
    """What the bot can honestly put in the MARKET CONTEXT block right now:
    the prefetched quote (QUOTE_PREFETCH_ENABLED), the AMFI cap tier and the
    session. Never blocks; a missing source just drops its line."""
    out: dict[str, Any] = {"session": session_of(filed_at)}
    try:
        from app.analyzer import quote_cache

        q = quote_cache.get_quote(symbol)
        lp = (q or {}).get("last_price")
        if lp:
            out["last_trade"] = float(lp)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.config import get_settings
        from app.services import mcap

        st = get_settings()
        tier = mcap.tier_of(mcap.lookup(symbol), float(st.CAP_LARGE_MIN_CR), float(st.CAP_MID_MIN_CR))
        if tier:
            out["cap_tier"] = tier
    except Exception:  # noqa: BLE001
        pass
    return out


def build_prompt(
    *,
    symbol: str,
    filed_at: str,
    headline: str,
    filing_text: str,
    session: Optional[str] = None,
    last_trade: Optional[float] = None,
    volume_ratio: Optional[float] = None,
    cap_tier: Optional[str] = None,
) -> tuple[str, str]:
    """Render the (system, user) pair in the shape the model trained on.

    The training prompt carried a MARKET CONTEXT block (last trade before
    the filing, pre-news volume ratio, market-cap tier, session). Serving
    it without that block was a train/serve skew: the model was read
    off-distribution on every filing. The analyzer now passes what it can
    measure at decision time: the prefetched quote, the AMFI cap tier and
    the session. Anything unknown is left out rather than faked, because a
    wrong number is worse than a missing one for a model that learned to
    read them.
    """
    parts = [
        f"SYMBOL: {symbol}",
        f"FILED: {filed_at}",
        f"HEADLINE: {headline}",
    ]
    ctx: list[str] = []
    if last_trade is not None and last_trade > 0:
        ctx.append(f"  {MARKET_CONTEXT_KEYS['last_trade']}: {float(last_trade):.2f}")
    if volume_ratio is not None and volume_ratio >= 0:
        ctx.append(f"  {MARKET_CONTEXT_KEYS['volume_ratio']}: {float(volume_ratio):.2f}")
    if cap_tier:
        ctx.append(f"  {MARKET_CONTEXT_KEYS['cap_tier']}: {cap_tier}")
    if session:
        ctx.append(f"  {MARKET_CONTEXT_KEYS['session']}: {session}")
    if ctx:
        parts += ["", "MARKET CONTEXT (as of the filing timestamp):", *ctx]
    parts += ["", "FILING:", filing_text or headline]
    return SYSTEM_PROMPT, "\n".join(parts)


def _map_event_type(raw: Any, headline: str, pdf_url: Optional[str]) -> str:
    value = str(raw or "").strip().upper()
    if value == "RESULTS":
        # The model has one RESULTS bucket; the bot distinguishes four
        # quarters plus annual, and the rules engine keys on that (declined
        # Q1_RESULTS filings move 56.7% of the time). Reuse the bot's own
        # headline detector rather than inventing a second one.
        detected = detect_event_type(headline or "", pdf_url)
        return detected if detected in _RESULTS_TYPES else "OTHER"
    return _EVENT_MAP.get(value, "OTHER")


def _sentiment_score(price_path: Any, direction: str) -> float:
    """t15 market-adjusted move -> the bot's -100..100 scale."""
    if isinstance(price_path, list) and len(price_path) > _T15:
        try:
            tenths = float(price_path[_T15])
        except (TypeError, ValueError):
            tenths = 0.0
        scaled = 100.0 * tenths / _SCORE_SATURATION_TENTHS
        return round(max(-100.0, min(100.0, scaled)), 1)
    # No usable path: fall back to the categorical direction rather than 0,
    # which would read as "measured neutral" instead of "not measured".
    return {"UP": 50.0, "DOWN": -50.0}.get(direction, 0.0)


def _confidence(mover: bool, materiality: str) -> float:
    """Derive a confidence the model does not emit.

    THIS IS A MAPPING, NOT A MODEL OUTPUT. The SFT targets carried no
    confidence field, so there is nothing calibrated to read. `mover` carries
    most of the weight because it is the only target this corpus was shown to
    be learnable on; materiality nudges it.

    Treat it with the same suspicion as DeepSeek's — whose confidence measured
    ANTI-predictive (ROC-AUC 0.42) on the same task. Run it through the HOLD
    calibration report before any rule thresholds on it.
    """
    conf = 0.75 if mover else 0.35
    conf += {"HIGH": 0.15, "MEDIUM": 0.05, "LOW": -0.05}.get(materiality, 0.0)
    return round(max(0.05, min(0.95, conf)), 2)


def to_analysis(
    content: str, *, headline: str = "", pdf_url: Optional[str] = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Model reply -> (AnalysisResponse-shaped dict, raw model JSON).

    Raises ValueError if the reply is not the JSON object the model was
    trained to emit; the caller stores that as an `invalid_json` failure
    exactly as it does for the hosted model.
    """
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object, got {type(raw).__name__}")

    direction = str(raw.get("direction") or "FLAT").strip().upper()
    mover = bool(raw.get("mover"))
    materiality = str(raw.get("materiality") or "UNKNOWN").strip().upper()
    facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}

    # Only a filing the model says will MOVE gets a direction. Predicting a
    # small drift is not a reason to take a position, and the rules engine
    # cannot express that nuance — so it is resolved here, once.
    recommendation = "HOLD"
    if mover and direction == "UP":
        recommendation = "BUY"
    elif mover and direction == "DOWN":
        recommendation = "SELL"

    amount = facts.get("amount_inr_cr")
    key_numbers: dict[str, Any] = {}
    if isinstance(amount, (int, float)):
        key_numbers["deal_value_inr_crore"] = float(amount)

    event_type = _map_event_type(raw.get("event_type"), headline, pdf_url)
    shape = str(raw.get("shape") or "").strip().upper()
    return {
        "event_type": event_type,
        "summary": (
            f"{raw.get('event_type', 'OTHER')}: model predicts {direction}"
            f"{' move' if mover else ' (no material move)'}"
            + (f", {shape.lower()} shape" if shape else "")
            + (f", Rs {amount:,.0f} crore" if isinstance(amount, (int, float)) else "")
        ),
        "sentiment": {"UP": "positive", "DOWN": "negative"}.get(direction, "neutral"),
        "sentiment_score": _sentiment_score(raw.get("price_path"), direction),
        "confidence": _confidence(mover, materiality),
        "recommendation": recommendation,
        "reasoning": (
            f"tradebot-slm-v1: materiality={materiality}, "
            f"surprise={raw.get('surprise', 'NONE')}, mover={mover}, "
            f"direction={direction}. Trained on measured 15-minute "
            f"market reaction, not on sentiment labels."
        ),
        "key_numbers": key_numbers,
    }, raw
