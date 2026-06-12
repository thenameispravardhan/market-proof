"""Fyers OAuth helpers.

The full flow:

    1. Operator visits `build_authorize_url(app_id, redirect_uri)`,
       which is `https://api-t1.fyers.in/api/v3/generate-authcode?...
       &redirect_uri=...&state=<csrf>`.
    2. They log in, approve the scopes, and Fyers redirects them to
       `redirect_uri?auth_code=<code>&state=<state>`.
    3. Our `/api/fyers/callback` endpoint receives the redirect, validates
       `state`, then calls `exchange_code_for_token(app_id, secret,
       code)` to swap the code for a long-lived access_token.
    4. The token is stored on the matching `broker_accounts` row.

The `state` value is a per-request CSRF nonce. We keep a per-process
dict `expected_states[state] -> created_at`; callbacks reject any
state that's not in the dict or that was issued more than 10 minutes
ago. This is "good enough" for a local-first app — a real prod
deployment would back this with Redis or signed cookies.

`exchange_code_for_token` returns a `TokenResponse` carrying the
access_token and a refresh hint. The Fyers v3 endpoint is:

    POST https://api-t1.fyers.in/api/v3/validate-authcode
    Content-Type: application/json
    {
      "grant_type": "authorization_code",
      "appIdHash": sha256(app_id + ":" + secret_key),
      "code": "<auth_code>"
    }

Successful response is `{"s": "ok", "code": 200, "access_token": "..."}`.

`build_app_id_hash` is exposed separately so tests can verify the
hash without a network call.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.logging_config import get_logger

log = get_logger(__name__)


FYERS_AUTH_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"
FYERS_TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
FYERS_API_BASE = "https://api-t1.fyers.in/api/v3"


def build_app_id_hash(app_id: str, secret_key: str) -> str:
    """SHA-256 of `<app_id>:<secret_key>` — Fyers' appIdHash format."""
    if not app_id or not secret_key:
        raise ValueError("app_id and secret_key are required")
    return hashlib.sha256(f"{app_id}:{secret_key}".encode("utf-8")).hexdigest()


def build_authorize_url(
    *,
    app_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Build the Fyers OAuth authorize URL.

    The operator opens this URL in a browser, logs in, and approves.
    Fyers redirects them to `redirect_uri?auth_code=<code>&state=<state>`.
    """
    if not app_id:
        raise ValueError("app_id is required")
    if not redirect_uri:
        raise ValueError("redirect_uri is required")
    if not state:
        raise ValueError("state (CSRF nonce) is required")
    from urllib.parse import urlencode

    qs = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{FYERS_AUTH_URL}?{qs}"


@dataclass
class TokenResponse:
    access_token: str
    raw: dict[str, Any]


class FyersAuthError(Exception):
    """Raised when the Fyers auth-code → token exchange fails."""

    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 body: Optional[str] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# Per-process expected-state registry for CSRF protection.
# In production this would be in Redis or a signed cookie. For a
# local-first app the process-local dict is enough.
_expected_states: dict[str, float] = {}
_STATE_TTL_S = 600.0  # 10 minutes


def register_state(state: str) -> None:
    """Mark a state value as expected. The callback validates against
    this registry."""
    if not state or not isinstance(state, str):
        raise ValueError("state must be a non-empty string")
    _expected_states[state] = time.monotonic()


def consume_state(state: str) -> bool:
    """If `state` is registered and fresh, consume it (one-shot) and
    return True. Otherwise return False."""
    issued_at = _expected_states.pop(state, None)
    if issued_at is None:
        return False
    if (time.monotonic() - issued_at) > _STATE_TTL_S:
        return False
    return True


def _clear_expired_states() -> None:
    """Drop states older than the TTL. Called opportunistically
    from `consume_state` and from tests."""
    now = time.monotonic()
    expired = [s for s, t in _expected_states.items() if (now - t) > _STATE_TTL_S]
    for s in expired:
        _expected_states.pop(s, None)


def reset_states() -> None:
    """Drop the in-process state registry. Tests use this between cases."""
    _expected_states.clear()


async def exchange_code_for_token(
    *,
    app_id: str,
    secret_key: str,
    code: str,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    timeout_s: float = 30.0,
) -> TokenResponse:
    """Swap an OAuth `code` for an access_token.

    The Fyers v3 validate-authcode endpoint takes:
        POST JSON { grant_type, appIdHash, code }
    and returns `{ s: 'ok', code: 200, access_token: '...' }` on success
    or `{ s: 'error', code: <int>, message: '...' }` on failure.

    `transport` lets tests inject `httpx.MockTransport(...)`. The
    `appIdHash` is computed via `build_app_id_hash` and is sent in
    the body — never in a header.
    """
    if not app_id or not secret_key or not code:
        raise FyersAuthError("app_id, secret_key, code are all required")
    app_id_hash = build_app_id_hash(app_id, secret_key)
    body = {
        "grant_type": "authorization_code",
        "appIdHash": app_id_hash,
        "code": code,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    transport = transport or httpx.AsyncHTTPTransport()
    async with httpx.AsyncClient(transport=transport, timeout=timeout_s) as client:
        try:
            resp = await client.post(FYERS_TOKEN_URL, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise FyersAuthError(f"timeout: {e!r}") from e
        except httpx.TransportError as e:
            raise FyersAuthError(f"transport error: {e!r}") from e

    if resp.status_code == 401:
        # Auth failed — wrong app_id / secret / code.
        raise FyersAuthError(
            "fyers auth failed: invalid credentials",
            status_code=401,
            body=_safe_body(resp),
        )
    if resp.status_code >= 400:
        raise FyersAuthError(
            f"fyers token endpoint returned {resp.status_code}",
            status_code=resp.status_code,
            body=_safe_body(resp),
        )
    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise FyersAuthError(
            f"fyers returned non-JSON: {e!r}",
            status_code=resp.status_code,
            body=_safe_body(resp),
        ) from e

    status_field = str(data.get("s") or "").lower()
    code_field = data.get("code")
    if status_field != "ok" or (isinstance(code_field, int) and code_field >= 400):
        raise FyersAuthError(
            f"fyers refused: {data.get('message') or data}",
            status_code=resp.status_code,
            body=_safe_body(resp),
        )

    access_token = data.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise FyersAuthError(
            "fyers response missing access_token",
            status_code=resp.status_code,
            body=_safe_body(resp),
        )
    return TokenResponse(access_token=access_token, raw=data)


def _safe_body(resp: httpx.Response) -> str:
    """Truncate response body for error messages. The access_token
    is never echoed back (it's the most sensitive field in the
    response and we don't want it in any log)."""
    try:
        text = resp.text
    except Exception:  # noqa: BLE001
        return "<unreadable>"
    if len(text) > 500:
        return text[:500] + "...<truncated>"
    return text


# One-shot CLI: print the Fyers auth URL. Operator opens it, logs in,
# copies the redirect URL, and POSTs to /api/fyers/callback.
def main() -> int:
    import sys
    from app.config import get_settings

    s = get_settings()
    if not s.FYERS_APP_ID or not s.FYERS_REDIRECT_URI:
        print(
            "FYERS_APP_ID and FYERS_REDIRECT_URI must be set in .env",
            file=sys.stderr,
        )
        return 1
    # Generate a state nonce and register it so /api/fyers/callback
    # accepts the redirect.
    import secrets
    state = secrets.token_urlsafe(24)
    register_state(state)
    url = build_authorize_url(
        app_id=s.FYERS_APP_ID,
        redirect_uri=s.FYERS_REDIRECT_URI,
        state=state,
    )
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
