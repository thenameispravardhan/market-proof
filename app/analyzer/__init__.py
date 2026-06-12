"""T3 — DeepSeek analyzer.

Subscribes to `announcements.new` (from T2's monitor), loads the
per-event-type prompt template (or DEFAULT), calls DeepSeek, validates
the JSON response, runs the rules engine against the `analyses` row,
persists a `signals` row, and publishes `signals.new` for T4.

Modules:
  - schemas        : pydantic models for the DeepSeek response
  - prompts        : template lookup, rendering, and the v1 seed set
  - deepseek_client: async httpx wrapper with retries + cost logging
  - rules_engine   : evaluates `signal_rules` rows against an analysis
  - service        : the event-bus loop that ties it all together

Public contracts (re-exported for tests and sibling tracks):
  - `EVENT_TYPES`      : the 15 supported event_type enum values
  - `EventType`        : enum of the 15 supported event_types
  - `Sentiment`        : enum
  - `Recommendation`   : enum
  - `AnalysisResponse` : pydantic model used to validate DeepSeek output
  - `Service`          : the analyzer loop (`start` / `stop` lifecycle)
"""
from app.analyzer.schemas import (
    EVENT_TYPES,
    AnalysisResponse,
    DEFAULT_EVENT_TYPE,
    EventType,
    KeyNumbers,
    Recommendation,
    Sentiment,
    analysis_to_dict,
)
from app.analyzer.service import CHANNEL_NEW_SIGNAL, Service

__all__ = [
    "EVENT_TYPES",
    "DEFAULT_EVENT_TYPE",
    "EventType",
    "Sentiment",
    "Recommendation",
    "KeyNumbers",
    "AnalysisResponse",
    "analysis_to_dict",
    "CHANNEL_NEW_SIGNAL",
    "Service",
]
