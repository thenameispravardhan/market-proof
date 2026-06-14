"""Webhook subsystem.

  - `signing`    — HMAC-SHA256 out / in
  - `dispatcher` — outbound fan-out to registered `webhooks` rows
                  on event-bus channels
  - `receiver`   — POST /api/webhooks/in/{webhook_id} — accepts an
                  external JSON payload, verifies signature,
                  persists an `analyses` + `signals` row, runs the
                  risk engine
  - `fyers`      — Fyers postback normalizer. Fyers' native
                  payload shape doesn't match the receiver's
                  generic contract; this module translates it.

This is the plugin / extension layer — distinct from
`app.notifications` (which is operator-curated, config-in-DB
notification channels for Telegram/Discord/email).
"""
from app.webhooks.signing import (
    compute_signature,
    verify_signature,
    SignatureError,
    REPLAY_WINDOW_SECONDS,
)
from app.webhooks.fyers import (
    is_fyers_postback,
    normalize_fyers_postback,
)

__all__ = [
    "compute_signature",
    "verify_signature",
    "SignatureError",
    "REPLAY_WINDOW_SECONDS",
    "is_fyers_postback",
    "normalize_fyers_postback",
]
