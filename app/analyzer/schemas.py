"""Pydantic schemas for the DeepSeek analyzer.

The DeepSeek response is a single JSON object. All fields below are
required by the model EXCEPT `key_numbers.*`, which are nullable
floats — the LLM omits them when the filing doesn't contain a number
of that kind (e.g. a dividend announcement has no deal value).

Design notes:

- `EventType` is a string enum (15 values) so adding a new category
  later is a one-line change.
- `Sentiment` and `Recommendation` are enums with the exact strings
  the signal-rules engine + downstream UI expect. The DB columns are
  String so we don't lock ourselves in to a particular DB enum.
- Ranges (`-100..100` for `sentiment_score`, `0..1` for `confidence`)
  are validated by pydantic — a bad LLM response is rejected loudly.
- `analysis_to_dict` is the canonical shape stored in
  `analyses.raw_response` and consumed by the rules engine (which
  works on plain dicts).
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# The 15 supported event_type categories + DEFAULT.
# These match the most common NSE / BSE corporate-filing categories and
# line up with the operator-editable prompt templates seeded by
# `scripts/seed_default_prompts.py`.
class EventType(str, Enum):
    ORDER_WIN = "ORDER_WIN"
    ACQUISITION = "ACQUISITION"
    MERGER = "MERGER"
    DIVIDEND = "DIVIDEND"
    BUYBACK = "BUYBACK"
    STOCK_SPLIT = "STOCK_SPLIT"
    BONUS = "BONUS"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    Q1_RESULTS = "Q1_RESULTS"
    Q2_RESULTS = "Q2_RESULTS"
    Q3_RESULTS = "Q3_RESULTS"
    Q4_RESULTS = "Q4_RESULTS"
    ANNUAL_RESULTS = "ANNUAL_RESULTS"
    BOARD_MEETING = "BOARD_MEETING"
    OTHER = "OTHER"


# The 15 + DEFAULT. (The `+ DEFAULT` is exposed as a separate constant
# for use in `seed_default_prompts.py` and prompt-template lookups.)
DEFAULT_EVENT_TYPE: str = "DEFAULT"
EVENT_TYPES: tuple[str, ...] = tuple(et.value for et in EventType) + (DEFAULT_EVENT_TYPE,)


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class Recommendation(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class KeyNumbers(BaseModel):
    """Numeric data points we care about for trading decisions.

    All four fields are optional — the LLM omits whichever doesn't
    apply to the filing. `deal_value_inr_crore` is the headline number
    for an ORDER_WIN / ACQUISITION; `stake_change_pct` is for
    ACQUISITION; `dividend_per_share` for DIVIDEND; `buyback_value_inr_crore`
    for BUYBACK. Each appears in the corresponding DB column on
    `analyses` as well.
    """
    model_config = ConfigDict(extra="ignore")

    deal_value_inr_crore: float | None = None
    stake_change_pct: float | None = None
    dividend_per_share: float | None = None
    buyback_value_inr_crore: float | None = None


class AnalysisResponse(BaseModel):
    """The JSON schema we ask DeepSeek to return.

    We hand this shape to the model as the description when we
    `response_format={"type": "json_object"}` is supported, and we
    validate the result with this class.

    Required fields: event_type, summary, sentiment, sentiment_score,
    confidence, recommendation, reasoning, key_numbers.

    Field semantics:
      - `event_type`  : one of the 15 EventType values; LLMs sometimes
                        pick OTHER when unsure — that's fine.
      - `summary`     : 1-2 sentence summary of the filing.
      - `sentiment`   : positive | neutral | negative.
      - `sentiment_score` : -100 (most negative) to +100 (most positive).
      - `confidence`  : 0.0 to 1.0 — model self-assessed certainty.
      - `recommendation` : BUY | SELL | HOLD.
      - `reasoning`   : chain-of-thought the UI can show to the operator.
      - `key_numbers` : the four numbers above; only fill in what applies.
    """

    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    event_type: EventType
    summary: str = Field(..., min_length=1)
    sentiment: Sentiment
    sentiment_score: float = Field(..., ge=-100.0, le=100.0)
    confidence: float = Field(..., ge=0.0, le=1.0)
    recommendation: Recommendation
    reasoning: str = Field(..., min_length=1)
    key_numbers: KeyNumbers

    @field_validator("summary", "reasoning")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v.strip()

    # LLMs are unreliable about casing ("buy" vs "BUY"); normalise
    # before enum validation rather than rejecting the analysis.
    @field_validator("recommendation", mode="before")
    @classmethod
    def _upper_enum(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().upper()
        return v

    @field_validator("event_type", mode="before")
    @classmethod
    def _event_type_or_other(cls, v: Any) -> Any:
        """Normalise casing AND map LLM-invented categories (e.g.
        'FUND_RAISING', 'APPOINTMENT') to OTHER. A novel label
        shouldn't throw away an otherwise-valid analysis — the
        sentiment / recommendation are still useful for trading."""
        if isinstance(v, str):
            up = v.strip().upper()
            return up if up in EventType.__members__ else EventType.OTHER.value
        return v

    @field_validator("sentiment", mode="before")
    @classmethod
    def _lower_enum(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("sentiment_score", mode="before")
    @classmethod
    def _normalise_score(cls, v: Any) -> Any:
        """Coerce a fractional sentiment_score onto the -100..100 scale.

        DeepSeek frequently returns the score on a 0..1 (or -1..1) scale
        despite the prompt — e.g. 0.95 for "very positive". Left as-is
        that 0.95 looks neutral to the rules engine and suppresses
        every trade. If the magnitude is <= 1 we treat it as a fraction
        and scale by 100. Genuine small integer scores (e.g. 5, -12)
        are outside [-1, 1] and pass through untouched. Exactly 0 stays
        neutral.
        """
        try:
            f = float(v)
        except (TypeError, ValueError):
            return v
        if f != 0.0 and -1.0 <= f <= 1.0:
            return f * 100.0
        return f

    def to_db_columns(self) -> dict[str, Any]:
        """Map the model onto the existing `analyses` DB columns.

        Used by `service.py` to construct the `Analysis` ORM row.
        Stored alongside the full `raw_response` JSON for round-trip
        fidelity.
        """
        kn = self.key_numbers
        return {
            "event_type": str(self.event_type),
            "sentiment": str(self.sentiment),
            "sentiment_score": float(self.sentiment_score),
            "confidence": float(self.confidence),
            "recommendation": str(self.recommendation),
            "rationale": self.reasoning,
            "summary": self.summary,
            "deal_value_inr_crore": kn.deal_value_inr_crore,
            "stake_change_pct": kn.stake_change_pct,
            "dividend_per_share": kn.dividend_per_share,
            "buyback_value_inr_crore": kn.buyback_value_inr_crore,
        }


def analysis_to_dict(a: AnalysisResponse) -> dict[str, Any]:
    """Convert the validated model to a plain dict — what gets stored
    in `analyses.raw_response` and what the rules engine evaluates
    against.

    The shape matches the per-column mapping (so a rule like
    `field="event_type"` and `value=["ORDER_WIN"]` works).
    """
    cols = a.to_db_columns()
    cols["key_numbers"] = a.key_numbers.model_dump()
    return cols
