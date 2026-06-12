"""HMAC-SHA256 signing for outbound webhooks and verification for inbound.

Scheme (matching the T6 spec):

  - Algorithm: HMAC-SHA256
  - Key:       the `webhook.secret` column
  - Digest:    hex(hmac.new(secret.encode("utf-8"), body, sha256).digest())
  - Header:    X-Mavis-Signature: <hex>

`compute_signature(secret, body)` is the single source of truth on
the outbound side; `verify_signature(secret, body, header)` is the
single source of truth on the inbound side. Both functions are
deterministic — same inputs always produce the same output — and
they NEVER log the secret or the signature header value in a way
that includes the secret (the redactor in `app/logging_config.py`
catches the rest as defence in depth).

Replay protection (inbound only): the payload MUST include a
`timestamp` field (epoch seconds, integer). The receiver rejects
anything older than `REPLAY_WINDOW_SECONDS`. The signature itself
covers the entire JSON body, so a replayed body has the same
signature as the original — the timestamp window is the
anti-replay mechanism.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional


REPLAY_WINDOW_SECONDS = 5 * 60  # 5 minutes per spec


class SignatureError(Exception):
    """Raised by `verify_signature` on any verification failure.

    Subclass `KeyError` is not appropriate here — this is a value
    mismatch, not a missing-key situation. The HTTP layer maps it
    to 401 (bad signature) or 400 (bad timestamp).
    """


def _digest(secret: str, body: bytes) -> str:
    return hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


def compute_signature(secret: str, body: bytes) -> str:
    """Return the lowercase hex digest of HMAC-SHA256(secret, body).

    The `secret` argument is never included in the returned value.
    The result is always 64 lowercase hex chars.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError(f"body must be bytes, got {type(body).__name__}")
    return _digest(secret, bytes(body))


def verify_signature(
    secret: str,
    body: bytes,
    header: Optional[str],
    *,
    constant_time: bool = True,
) -> bool:
    """Return True iff `header` matches `compute_signature(secret, body)`.

    Missing / malformed header → False. The constant-time comparison
    uses `hmac.compare_digest` to avoid timing attacks.
    """
    if not header or not isinstance(header, str):
        return False
    expected = _digest(secret, body)
    # Accept the header with or without a `sha256=` prefix (some
    # webhook ecosystems use the `sha256=...` form, like GitHub).
    candidate = header.strip()
    if candidate.startswith("sha256="):
        candidate = candidate[len("sha256=") :]
    if constant_time:
        return hmac.compare_digest(expected, candidate.lower())
    return expected == candidate.lower()


def extract_timestamp(payload: dict[str, Any]) -> Optional[int]:
    """Read a `timestamp` (epoch seconds) from the payload.

    Returns None if the field is missing or not an integer.
    Accepts both int and string (digits only).
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("timestamp")
    if isinstance(raw, bool):
        # bool is an int subclass — reject explicitly.
        return None
    if isinstance(raw, int):
        return int(raw)
    if isinstance(raw, str) and raw.strip().isdigit():
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def is_fresh_timestamp(payload: dict[str, Any], *, now: Optional[float] = None) -> bool:
    """Return True iff `payload['timestamp']` is within
    REPLAY_WINDOW_SECONDS of `now` (default: time.time()).

    Returns False on any error (missing/non-numeric/out-of-window).
    A future timestamp is allowed up to +REPLAY_WINDOW_SECONDS/2
    to absorb clock skew between sender and receiver.
    """
    ts = extract_timestamp(payload)
    if ts is None:
        return False
    cur = float(now if now is not None else time.time())
    skew = (cur - float(ts))
    return -REPLAY_WINDOW_SECONDS / 2 <= skew <= REPLAY_WINDOW_SECONDS
