"""Fyers postback normalizer.

Fyers sends order-update POSTs to a URL you register in the Fyers API
dashboard. The native payload shape is broker-specific and does NOT
match the generic inbound receiver contract in `app.webhooks.receiver`
(which requires `event_type`, `symbol`, `action`).

This module:
  - Detects whether an inbound payload looks like a Fyers postback.
  - Translates it to the receiver's expected shape.
  - Preserves the original Fyers payload as `raw_fyers` so it's
    available in `analyses.raw_response.raw_payload` for audit and
    downstream consumers (the trade journal in particular).

Detection: a payload is treated as a Fyers postback iff it has at
least one of the Fyers-specific keys (`order_id` + `status` + a
symbol field, OR an explicit `_fyers=1` marker the dispatcher can add
when fanning out via a dedicated Fyers webhook row).

Mapping rules:
  - `event_type`    = "FYERS_ORDER_UPDATE"
  - `symbol`        = the Fyers symbol (e.g. "NSE:SBIN-EQ" or
                     "SBIN-EQ"). We also split out the exchange prefix
                     into `exchange` ("NSE" / "BSE" / "MCX").
  - `action`        = "HOLD" — order updates are informational, not
                     trade triggers. Fyers postback should never be
                     used to drive BUY/SELL decisions (that's a
                     feedback loop).
  - `timestamp`     = now (Fyers postbacks have `orderTimestamp` but
                     the receiver needs a fresh epoch for replay
                     protection).
  - `headline`      = "Fyers {status} {symbol} order_id={id}"
  - `summary`       = the same, with extra context fields if present.
  - `confidence`    = 0.5 (neutral; this is an info event, not a
                     signal).
  - `sentiment`     = "neutral".
  - `key_numbers`   = best-effort copy of `tradedPrice`, `qty`,
                     `tradedValue`, `orderType`, `productType`,
                     `limitPrice`, `stopPrice`, etc.

The normalizer is intentionally a PURE function. Tests can drive it
without HTTP, a DB, or a webhook row.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional


# Field names Fyers typically uses in its postback payload. We match on
# a few of these to detect "this looks like a Fyers payload" without
# being too strict (Fyers has tweaked field names across versions).
FYERS_DETECT_FIELDS: tuple[str, ...] = (
    "order_id",       # v3 postback
    "id",             # alternate
    "orderTimestamp",
    "tradedPrice",
    "productType",
    "orderType",
)

# Some payload shapes include a list `orders` rather than a single
# object. We support that too: normalize each entry and pick the first
# one (the receiver can only process one signal at a time anyway).

# Status → display text + a coarse-grained signal-affecting flag.
# The receiver doesn't act on the signal (HOLD) so this is mostly
# for the journal / dashboard.
FYERS_STATUS_HINTS: dict[str, str] = {
    "1": "CANCELLED",
    "2": "FILLED",
    "4": "REJECTED",
    "5": "AMO_MODIFIED",
    "6": "AMO_CANCELLED",
    "7": "MODIFIED",
    "8": "EXPIRED",
    "10": "TRIGGERED",
    "11": "AMO_FROZEN",
}


def _split_symbol(raw: str) -> tuple[str, str]:
    """Split a Fyers-style symbol like "NSE:SBIN-EQ" into
    ("NSE", "SBIN-EQ"). Defaults to ("NSE", raw) when no prefix."""
    if not raw:
        return ("NSE", "")
    s = str(raw).strip()
    if ":" in s:
        exch, _, sym = s.partition(":")
        return (exch.strip().upper(), sym.strip())
    return ("NSE", s)


def _status_text(raw: Any) -> str:
    """Map a Fyers numeric status code to its text label. Falls back
    to the raw value as a string when it doesn't match the known set."""
    if raw is None:
        return "UNKNOWN"
    key = str(raw).strip()
    if key in FYERS_STATUS_HINTS:
        return FYERS_STATUS_HINTS[key]
    if key.isalpha():
        return key.upper()
    return key


def is_fyers_postback(payload: Any) -> bool:
    """Return True iff the payload shape looks like a Fyers postback.

    Detection is intentionally conservative — false positives just
    route a non-Fyers payload through the Fyers normalizer (the worst
    case is that the original payload gets wrapped under `raw_fyers`
    and `action` becomes HOLD). The receiver contract still applies
    on the OUTPUT payload, so a non-Fyers-shaped input will simply
    fail downstream validation."""
    if not isinstance(payload, dict):
        return False
    # Explicit marker (so the dispatcher / a future wrapper can
    # force-trigger the Fyers path).
    if payload.get("_fyers") in (1, "1", True):
        return True
    # Heuristic: at least two Fyers-typical fields present.
    hits = sum(1 for f in FYERS_DETECT_FIELDS if f in payload)
    if hits >= 2:
        return True
    # Single-orders-list shape (Fyers sometimes wraps in `orders`).
    if "orders" in payload and isinstance(payload["orders"], list):
        if any(isinstance(o, dict) and ("order_id" in o or "id" in o) for o in payload["orders"]):
            return True
    return False


def _unwrap_fyers(payload: Mapping[str, Any]) -> dict[str, Any]:
    """If the payload wraps the actual order object under `orders` (a
    list), return the first entry. Otherwise return the payload
    unchanged."""
    if "orders" in payload and isinstance(payload["orders"], list) and payload["orders"]:
        first = payload["orders"][0]
        if isinstance(first, dict):
            # Merge: list-level fields first, then order-level fields
            # (order-level wins on collisions).
            merged: dict[str, Any] = {k: v for k, v in payload.items() if k != "orders"}
            merged.update(first)
            return merged
    return dict(payload)


def _parse_fyers_timestamp(raw: Any) -> Optional[int]:
    """Fyers' `orderTimestamp` is a string like "13-Jun-2026 12:30:15"
    OR an epoch millisecond int. Return epoch SECONDS or None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # Heuristic: > 10^12 means milliseconds.
        secs = float(raw)
        if secs > 1e12:
            secs = secs / 1000.0
        return int(secs)
    if isinstance(raw, str):
        s = raw.strip()
        if s.isdigit():
            return _parse_fyers_timestamp(int(s))
        # Try a few common formats Fyers has used over the years.
        for fmt in (
            "%d-%b-%Y %H:%M:%S",
            "%d-%b-%Y %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                return int(dt.replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                continue
    return None


def _build_key_numbers(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Pull out the numeric / structural fields that the Analysis
    schema's `key_numbers` accepts."""
    out: dict[str, Any] = {}
    # Direct numeric fields
    for k_in, k_out in (
        ("tradedPrice", "traded_price"),
        ("limitPrice", "limit_price"),
        ("stopPrice", "stop_price"),
        ("tradedValue", "traded_value"),
        ("qty", "quantity"),
        ("remainingQuantity", "remaining_quantity"),
    ):
        v = raw.get(k_in)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k_out] = float(v)
    # Categorical fields
    for k in ("orderType", "productType", "transactionType", "exchange", "segment"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def normalize_fyers_postback(payload: Any) -> dict[str, Any]:
    """Translate a Fyers postback payload into the shape the generic
    inbound receiver expects.

    Returns a new dict. The original payload is preserved under
    `raw_fyers` for audit / UI display. The `timestamp` field is
    always set to a fresh epoch second (Fyers' own timestamps are
    kept under `raw_fyers.orderTimestamp` but the receiver's replay
    window is measured against the current clock, not the order
    time).
    """
    if not isinstance(payload, dict):
        # Not a dict — return a minimal payload that will fail the
        # generic shape check downstream rather than crashing here.
        return {
            "event_type": "FYERS_ORDER_UPDATE",
            "symbol": "",
            "action": "HOLD",
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "summary": "Fyers postback: non-dict payload",
            "raw_fyers": payload,
        }

    inner = _unwrap_fyers(payload)
    raw_symbol = str(inner.get("symbol") or inner.get("fyCode") or inner.get("instrument") or "").strip()
    exchange, symbol = _split_symbol(raw_symbol)
    if not symbol:
        # Last-ditch: many Fyers payloads carry the symbol under
        # `fyCode` + exchange under `exchange` separately.
        if inner.get("fyCode"):
            symbol = str(inner["fyCode"]).strip()
        if inner.get("exchange") and not exchange:
            exchange = str(inner["exchange"]).strip().upper()

    order_id = inner.get("order_id") or inner.get("id") or inner.get("orderNumber")
    status_text = _status_text(inner.get("status"))
    order_type = inner.get("orderType")
    txn_type = inner.get("transactionType") or inner.get("side")

    summary = (
        f"Fyers postback: status={status_text}"
        f" symbol={symbol or raw_symbol}"
        f" order_id={order_id}"
        + (f" txn={txn_type}" if txn_type else "")
        + (f" type={order_type}" if order_type else "")
    )
    headline = (
        f"Fyers {status_text} {symbol or raw_symbol}"
        + (f" ({order_id})" if order_id else "")
    )

    return {
        "event_type": "FYERS_ORDER_UPDATE",
        "symbol": symbol or raw_symbol or "UNKNOWN",
        "exchange": exchange,
        "action": "HOLD",  # postback is informational; never a trade trigger
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "headline": headline,
        "summary": summary,
        "confidence": 0.5,
        "sentiment": "neutral",
        "sentiment_score": 0.0,
        "reasoning": summary,
        "key_numbers": _build_key_numbers(inner),
        # Original payload — useful for the trade journal, debugging,
        # and any downstream consumer that wants the raw fields.
        "raw_fyers": inner,
    }
