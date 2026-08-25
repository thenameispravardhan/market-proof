"""Self-describing settings registry, derived from `app.config.Settings`.

WHY THIS EXISTS
---------------
Every operator knob used to be declared in four places: `config.py`, the
`_GLOBAL_KEYS` dict in `settings_api.py`, `GlobalSettings` in the frontend's
`types.ts`, and a hand-written `FieldSpec` in a React component. Miss any one
and the knob is invisible in the UI — which is how 67 settings ended up
unreachable from the frontend, and how `_GLOBAL_KEYS` drifted from `config.py`
on seven defaults while claiming to mirror it.

So the list is no longer hand-maintained. `Settings.model_fields` IS the
registry: pydantic already knows every key, its type and its default. This
module adds only what pydantic cannot infer — which group a key belongs to,
a human label, sane widget bounds, and whether a restart is needed — and the
frontend renders whatever it is handed.

Adding a knob is now ONE edit: declare it in `config.py`. It appears in the UI
with a derived label, in the group its prefix maps to. Give it a nicer label or
tighter bounds here only if the derived ones are wrong.

`app/tests/test_settings_schema.py` fails the build if a new `Settings` field
is neither exposed nor deliberately excluded, so this can never silently drift
again.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from app.config import Settings

# ---------------------------------------------------------------------------
# Exclusions — the only keys that are NOT operator-editable.
# ---------------------------------------------------------------------------

# Credentials. Edited through PUT /api/settings/credentials, which writes .env
# and never returns the secret. Exposing them here would leak them via GET.
_SECRET_KEYS = frozenset({
    "DEEPSEEK_API_KEY",
    "FYERS_APP_ID",
    "FYERS_SECRET_KEY",
    "FYERS_ACCESS_TOKEN",
    "FYERS_REDIRECT_URI",
    "FYERS_POSTBACK_SECRET",
})

# Process/boot config. Changing these at runtime cannot take effect (the socket
# is already bound, the engine already connected) so a UI control would lie.
_BOOT_KEYS = frozenset({
    "DATABASE_URL",
    "BIND_HOST",
    "BIND_PORT",
    "TESTING",
    "APP_VERSION",
})

EXCLUDED = _SECRET_KEYS | _BOOT_KEYS

# Editable, but only through their own guarded endpoint. Rendered read-only so
# the generic form cannot bypass the confirmation flow.
_READ_ONLY = frozenset({
    # POST /api/settings/trading-mode requires the operator to type "LIVE".
    "TRADING_MODE",
})

# Read once at startup by the component that owns them; a UI edit persists and
# survives the restart, but does not take effect until then. Flagged, not
# hidden — an honest badge beats a missing control.
RESTART_REQUIRED = frozenset({
    "LOG_LEVEL",
    "LLM_MAX_RETRIES",
    "LLM_RETRY_BACKOFF_SECONDS",
})

# ---------------------------------------------------------------------------
# Groups — ordered. First matching prefix wins, so put specific before general
# (SENTIMENT_DECAY_* must beat the bare "sentiment" idea, DEFAULT_SL_* must beat
# nothing else). A key matching no prefix lands in "Other", which is a prompt to
# come back and classify it, not an error.
# ---------------------------------------------------------------------------

GROUPS: list[dict[str, Any]] = [
    {
        "id": "capital",
        "title": "Capital & risk",
        "note": "How much of the account a single trade — or the whole day — may put at risk.",
        "prefixes": [
            "PORTFOLIO_VALUE", "MAX_CAPITAL_RISK_PCT", "DAILY_MAX_LOSS_PCT",
            "MAX_CONCURRENT_POSITIONS", "MAX_SINGLE_POSITION_PCT", "MIN_LIQUIDITY_CRORE",
            "MAX_SIGNALS_PER_DAY", "MAX_TRADES_PER_DAY", "INTRADAY_LEVERAGE", "RISK_RAMP_",
        ],
    },
    {
        "id": "breakers",
        "title": "Circuit breakers",
        "note": "Loss limits that throttle or halt trading. Tripping the daily or monthly "
                "breaker flattens and stops; weekly throttles risk per trade.",
        "prefixes": [
            "WEEKLY_MAX_LOSS_PCT", "MONTHLY_MAX_DRAWDOWN_PCT", "WEEKLY_BREACH_RISK_PCT",
            "MAX_CONSECUTIVE_LOSERS", "CONSECUTIVE_LOSER_",
        ],
    },
    {
        "id": "concentration",
        "title": "Concentration & clustering",
        "note": "Caps that stop one news event filling the book with correlated names.",
        "prefixes": ["SECTOR_", "MAX_POSITIONS_PER_SECTOR"],
    },
    {
        "id": "filters",
        "title": "Pre-trade filters",
        "note": "Conditions a signal must clear before it can become an order. Filters whose "
                "feed is not yet wired no-op rather than fake a pass.",
        "prefixes": [
            "MIN_SENTIMENT_CONFIDENCE", "INDIA_VIX_MAX", "MAX_SPREAD_PCT",
            "SHORTING_ENABLED", "REQUIRE_KNOWN_LIQUIDITY",
        ],
    },
    {
        "id": "detection",
        "title": "Detection & speed",
        "note": "How fast filings are picked up and how stale one may be before it is dropped.",
        "prefixes": [
            "POLL_INTERVAL_SECONDS", "MAX_NEWS_AGE_", "NEWS_AGE_FROM_RECEIPT",
            "PIPELINE_DEADLINE_SECONDS", "QUOTE_REFRESH_SECONDS", "FYERS_STREAMING_ENABLED",
        ],
    },
    {
        "id": "sources",
        "title": "News sources",
        "note": "Each source is an independent monitor. Detection speed is the fastest "
                "enabled source; duplicates are collapsed.",
        "prefixes": ["NSE_API_ENABLED", "BSE_API_ENABLED", "NSE_RSS_"],
    },
    {
        "id": "ai",
        "title": "AI analysis",
        "note": "What the analyzer is allowed to spend, and how much of the filing it sees.",
        "prefixes": [
            "AI_ANALYSIS_ENABLED", "PRE_LLM_FILTER_ENABLED", "SEND_EXTRACTED_TEXT",
            "FAST_TRACK_ENABLED", "LLM_", "PDF_",
        ],
    },
    {
        "id": "entry",
        "title": "Entry execution",
        "note": "How an approved signal becomes a filled order — pricing, drift and timeouts.",
        "prefixes": [
            "ENTRY_", "MAX_SLIPPAGE_PCT", "ORDER_FILL_TIMEOUT_SECONDS",
            "RETRACEMENT_", "SYMBOL_LOCK_WAIT_SECONDS", "BROKER_ORDER_TIMEOUT_SECONDS",
        ],
        # ENTRY_WINDOW_*_IST are session times, not execution — reassigned below.
        "exclude": ["ENTRY_WINDOW_START_IST", "ENTRY_WINDOW_END_IST"],
    },
    {
        "id": "exit_exec",
        "title": "Exit execution",
        "note": "How an exit reaches the broker, and the safety rails around a stale feed.",
        "prefixes": [
            "EXIT_", "STALE_QUOTE_SECONDS", "POSITION_RECONCILE_SECONDS",
        ],
    },
    {
        "id": "exit_rules",
        "title": "Exit rules",
        "note": "Stops, targets, trailing and momentum-death exits. The Exits page is the "
                "richer editor for these.",
        "prefixes": [
            "DEFAULT_SL_", "DEFAULT_TARGET_RR", "SMALLCAP_PRICE", "ATR_",
            "BREAKEVEN_", "SCALE_OUT_", "TRAIL_", "CONSOLIDATION_", "STALL_",
            "MAX_HOLD_SECONDS",
        ],
    },
    {
        "id": "session",
        "title": "Market session",
        "note": "IST clock. The square-off can be moved earlier but never disabled — "
                "this strategy is intraday-only.",
        "prefixes": [
            "MARKET_OPEN_IST", "MARKET_CLOSE_IST", "ENTRY_WINDOW_START_IST",
            "ENTRY_WINDOW_END_IST", "SQUARE_OFF_TIME_IST", "ENFORCE_MARKET_HOURS",
        ],
    },
    {
        "id": "model",
        "title": "Mover model",
        "note": "Offline-trained P(mover), scored live from live_model.json. "
                "MODEL_ENABLED only surfaces the score; MODEL_GATE_ENABLED is "
                "what lets a low score veto a trade. Tune on the Model page.",
        "prefixes": ["MODEL_"],
    },
    {
        "id": "perf_sizer",
        "title": "Performance-weighted sizing",
        "note": "Tiers per-trade risk on each event type's realised record. Below the "
                "minimum trade count the base risk applies unchanged.",
        "prefixes": ["PERF_SIZER_"],
    },
    {
        "id": "decay",
        "title": "Sentiment decay",
        "note": "The target multiple shrinks as news ages at order time. The stop is never "
                "widened — only the reward expectation comes in.",
        "prefixes": ["SENTIMENT_DECAY_"],
    },
    {
        "id": "dataset",
        "title": "Dataset builder",
        "note": "Candle-reaction enrichment for the ML dataset. Pure telemetry — no "
                "trading influence.",
        "prefixes": ["DATASET_"],
    },
    {
        "id": "telemetry",
        "title": "Telemetry & reports",
        "note": "Outcome logging and the daily health report. No trading influence.",
        "prefixes": ["OUTCOME_LOGGER_ENABLED", "HEALTH_REPORT_", "LOG_LEVEL"],
    },
    {
        "id": "mode",
        "title": "Trading mode",
        "note": "Paper or live. Switching to live requires typing LIVE on the Dashboard.",
        "prefixes": ["TRADING_MODE"],
    },
]

_OTHER_GROUP = {"id": "other", "title": "Other", "note": "Not yet classified.", "prefixes": []}


# ---------------------------------------------------------------------------
# Explicit overrides — ONLY where the derived value would be wrong or unsafe.
# ---------------------------------------------------------------------------

# Bounds tighter (or wider) than the suffix heuristic produces. These are the
# guards that used to live inline in settings_api._coerce; they are the UI door,
# and several are stricter than Settings' own validators on purpose.
BOUNDS: dict[str, tuple[float, float]] = {
    # Probabilities and coverage fractions, not percents.
    "MODEL_MIN_PROBABILITY": (0.0, 1.0),
    "MODEL_MIN_COVERAGE": (0.0, 1.0),
    "INTRADAY_LEVERAGE": (1.0, 10.0),          # 1x unleveraged, Fyers MIS ~5x
    # R-multiples: must be positive, and >10 is a typo not a strategy.
    "ATR_STOP_MULT": (0.1, 10),
    "SCALE_OUT_R": (0.1, 10),
    "TRAIL_ACTIVATE_R": (0.1, 10),
    "TRAIL_DISTANCE_R": (0.1, 10),
    "MAX_HOLD_SECONDS": (60, 23400),           # sub-minute forced exit is churn
    "CONSOLIDATION_WINDOW_SECONDS": (10, 3600),
    "STALL_WINDOW_SECONDS": (10, 3600),
    "PIPELINE_DEADLINE_SECONDS": (0, 300),     # 0 = disabled
    "MAX_NEWS_AGE_ABSOLUTE_SECONDS": (0, 86400),
    "NSE_RSS_POLL_SECONDS": (0, 60),           # 0 = follow the global interval
    "POLL_INTERVAL_SECONDS": (1, 3600),
    "LLM_MAX_TOKENS": (250, 4000),             # <250 truncates the JSON → lost signal
    "ATR_PERIOD": (2, 200),
    "PORTFOLIO_VALUE": (1000, 1e12),
    # 0..1 confidence scales, not percentages.
    "MIN_SENTIMENT_CONFIDENCE": (0.05, 1.0),
    "CONSECUTIVE_LOSER_RESUME_CONFIDENCE": (0.05, 1.0),
    "PERF_SIZER_HIGH_WIN_RATE": (0.0, 1.0),
    "PERF_SIZER_LOW_WIN_RATE": (0.0, 1.0),
}

# Clock fields the operator may move but never disable. Enforced in _coerce.
TIME_BOUNDS_IST: dict[str, tuple[str, str]] = {
    # Earlier is fine; later than 15:15 collides with the 15:30 close and the
    # broker's own MIS auto-square-off. There is deliberately no "off".
    "SQUARE_OFF_TIME_IST": ("09:30", "15:15"),
}

# Where the auto-generated label reads badly.
LABELS: dict[str, str] = {
    "PORTFOLIO_VALUE": "Portfolio value (₹)",
    "MIN_LIQUIDITY_CRORE": "Min liquidity (₹ crore)",
    "INTRADAY_LEVERAGE": "Intraday leverage (×)",
    "LLM_MAX_TOKENS": "AI max output tokens",
    "LLM_TIMEOUT_SECONDS": "AI call timeout (seconds)",
    "LLM_MAX_RETRIES": "AI retries",
    "LLM_RETRY_BACKOFF_SECONDS": "AI retry backoff (seconds)",
    "PDF_FETCH_TIMEOUT_SECONDS": "Filing PDF fetch timeout (seconds)",
    "PDF_MAX_TEXT_CHARS": "Filing text budget (characters)",
    "AI_ANALYSIS_ENABLED": "AI news analysis",
    "PRE_LLM_FILTER_ENABLED": "Pre-AI noise filter",
    "SEND_EXTRACTED_TEXT": "Send extracted PDF text to AI",
    "NEWS_AGE_FROM_RECEIPT": "Measure news age from receipt",
    "ENTRY_MAX_DRIFT_PCT": "Max entry drift against the signal (%)",
    "SCALE_OUT_R": "Scale-out at (R)",
    "TRAIL_ACTIVATE_R": "Arm trailing stop at (R)",
    "TRAIL_DISTANCE_R": "Trail distance (R)",
    "ATR_STOP_MULT": "ATR stop multiple (×)",
    "MODEL_ENABLED": "Score filings with the mover model (telemetry only)",
    "MODEL_GATE_ENABLED": "Let a low model score BLOCK a trade",
    "MODEL_VARIANT": "Model variant (blank = artifact default)",
    "MODEL_MIN_PROBABILITY": "Min P(mover) to allow (0-1)",
    "MODEL_MIN_COVERAGE": "Min feature coverage to gate on (0-1, 0 = never abstain)",
    "NSE_RSS_POLL_SECONDS": "NSE RSS poll (seconds, 0 = follow global)",
    "PIPELINE_DEADLINE_SECONDS": "Signal deadline (seconds, 0 = off)",
    "MAX_NEWS_AGE_ABSOLUTE_SECONDS": "Absolute news age ceiling (seconds, 0 = off)",
    "ENFORCE_MARKET_HOURS": "Enforce market hours",
    "OUTCOME_LOGGER_ENABLED": "Signal outcome tracking",
    "SHORTING_ENABLED": "Allow short entries",
}

_WORD_FIXUPS = {
    "Pct": "(%)", "Ist": "(IST)", "Atr": "ATR", "Llm": "AI", "Ai": "AI",
    "Pdf": "PDF", "Nse": "NSE", "Bse": "BSE", "Rss": "RSS", "Vix": "VIX",
    "Roc": "rate-of-change", "Rr": "reward:risk", "Kmp": "KMP",
    "Seconds": "(seconds)", "Minutes": "(minutes)", "Days": "(days)",
}


def _humanize(key: str) -> str:
    """MAX_CAPITAL_RISK_PCT -> 'Max capital risk (%)'."""
    if key in LABELS:
        return LABELS[key]
    words = [w.capitalize() for w in key.split("_")]
    words = [_WORD_FIXUPS.get(w, w.lower()) for w in words]
    if words:
        first = words[0]
        words[0] = first if first.isupper() or first.startswith("(") else first.capitalize()
    return " ".join(words).replace(" (%)", " (%)").strip()


def _widget(key: str, annotation: Any) -> tuple[str, Optional[float], Optional[float], Optional[float]]:
    """Return (widget, min, max, step) for a key. Bounds come from BOUNDS when
    present, else from the name suffix, which encodes the unit by convention."""
    if annotation is bool:
        return "toggle", None, None, None
    if key.endswith("_IST"):
        return "time", None, None, None
    if annotation is str:
        return "text", None, None, None

    is_float = annotation is float
    if key in BOUNDS:
        lo, hi = BOUNDS[key]
    elif key.endswith("_PCT"):
        lo, hi = 0.0, 100.0
    elif key.endswith(("_SECONDS", "_MINUTES")):
        lo, hi = 0.0, 86400.0
    elif key.endswith(("_R", "_MULT")):
        lo, hi = 0.0, 10.0
    elif key.endswith("_DAYS"):
        lo, hi = 0.0, 3650.0
    else:
        lo, hi = 0.0, 1_000_000.0

    if key.endswith("_PCT") or (is_float and hi <= 10):
        step: float = 0.1
    elif is_float:
        step = 0.5
    else:
        step = 1.0
    return "number", lo, hi, step


def build_registry() -> dict[str, dict[str, Any]]:
    """Every operator-editable setting, derived from `Settings.model_fields`."""
    out: dict[str, dict[str, Any]] = {}
    for key, field in Settings.model_fields.items():
        if key in EXCLUDED:
            continue
        ann = field.annotation
        # Literal["paper","live"] and friends carry their choices in __args__.
        choices = list(getattr(ann, "__args__", ())) if getattr(ann, "__origin__", None) is Literal else None
        typ: Any = str if choices else ann
        widget, lo, hi, step = _widget(key, typ)
        if choices:
            widget = "select"
        out[key] = {
            "key": key,
            "type": {bool: "bool", int: "int", float: "float", str: "str"}.get(typ, "str"),
            "default": field.default,
            "label": _humanize(key),
            "widget": widget,
            "min": lo,
            "max": hi,
            "step": step,
            "choices": choices,
            "group": _group_of(key),
            "read_only": key in _READ_ONLY,
            "restart_required": key in RESTART_REQUIRED,
        }
    return out


def _group_of(key: str) -> str:
    for group in GROUPS:
        if key in group.get("exclude", []):
            continue
        for prefix in group["prefixes"]:
            if key == prefix or key.startswith(prefix):
                return group["id"]
    return _OTHER_GROUP["id"]


def schema_payload(effective: dict[str, Any]) -> list[dict[str, Any]]:
    """Grouped field specs plus current values — everything the UI needs to
    render the whole settings surface without knowing a single key name."""
    registry = build_registry()
    by_group: dict[str, list[dict[str, Any]]] = {}
    for key, spec in registry.items():
        by_group.setdefault(spec["group"], []).append({**spec, "value": effective.get(key, spec["default"])})

    payload = []
    for group in [*GROUPS, _OTHER_GROUP]:
        fields = sorted(by_group.get(group["id"], []), key=lambda f: f["key"])
        if not fields:
            continue
        payload.append({
            "id": group["id"],
            "title": group["title"],
            "note": group["note"],
            "fields": fields,
        })
    return payload
