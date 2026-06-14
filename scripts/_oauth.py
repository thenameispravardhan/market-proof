"""Fyers OAuth automation.

The flow the operator normally has to do by hand:

  1. Start the bot.
  2. Visit GET /api/fyers/authorize-url to get a one-time URL.
  3. Open that URL in a browser.
  4. Log into Fyers, approve the scopes.
  5. Fyers redirects to /api/fyers/callback?auth_code=...&state=...
  6. The bot exchanges the code for a token, writes it to
     broker_accounts.access_token, and adds an audit_log row with
     action='fyers.token_rotated'.

This module collapses all of that into one call:

    result = run_oauth_flow(bot_url=..., timeout_s=180)

It:
  - GETs `/api/fyers/authorize-url` from the running bot to get a
    fresh `state` (registered server-side) and the authorize URL.
  - Opens the URL in the operator's default browser.
  - Polls the bot's `/api/fyers/status` endpoint until the
    connection flips to `connected` (i.e. an access token is
    present on the matching broker_accounts row).
  - Returns a small dataclass with the outcome.

Tests use `webbrowser.open = MagicMock()` and a fake `httpx.Client`
that flips the status after one call.
"""
from __future__ import annotations

import time
import webbrowser
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class OAuthOutcome:
    ok: bool
    public_url: str           # the bot's public base URL (after the tunnel)
    authorize_url: str        # the Fyers URL we opened in the browser
    state: str                # CSRF nonce
    account_id: Optional[int] = None
    app_id: Optional[str] = None
    reason: Optional[str] = None


class OAuthError(Exception):
    """The OAuth flow didn't complete in time or the bot refused."""


# A small `httpx.Client` factory hook for tests so we don't have to
# monkey-patch the module global. Production code uses the default
# (real) httpx.Client.
_ClientFactory = Any


def _default_client_factory() -> _ClientFactory:
    return httpx.Client(timeout=15.0)


def _get_authorize_url(bot_public_url: str, client: _ClientFactory) -> dict[str, Any]:
    """GET /api/fyers/authorize-url on the running bot. The bot
    registers a CSRF `state` and returns the Fyers URL to open."""
    r = client.get(f"{bot_public_url.rstrip('/')}/api/fyers/authorize-url")
    r.raise_for_status()
    return r.json()


def _poll_fyers_status(
    bot_public_url: str,
    client: _ClientFactory,
    *,
    timeout_s: float,
    interval_s: float = 1.5,
) -> dict[str, Any]:
    """Poll /api/fyers/status until connected=True OR timeout.

    We treat "connected" as `configured AND account_present AND
    access_token_present` — the same shape the UI banner uses
    (see app/api/fyers_callback.py `fyers_status`)."""
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            r = client.get(f"{bot_public_url.rstrip('/')}/api/fyers/status")
            r.raise_for_status()
            last = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("oauth.status_poll_failed", error=str(e))
            time.sleep(interval_s)
            continue
        if last.get("connected") is True:
            return last
        time.sleep(interval_s)
    raise OAuthError(
        f"OAuth flow did not complete within {timeout_s:.0f}s. "
        f"Last status: {last}"
    )


def run_oauth_flow(
    *,
    bot_public_url: str,
    timeout_s: float = 180.0,
    open_browser: bool = True,
    client_factory: Optional[_ClientFactory] = None,
) -> OAuthOutcome:
    """Run the full OAuth round-trip.

    Args:
      bot_public_url: the URL Fyers will redirect to. In dev this is
        the cloudflared/ngrok HTTPS URL of the running bot.
      timeout_s: how long to wait for the operator to complete the
        browser-side login.
      open_browser: if True, call `webbrowser.open(...)` to launch
        the default browser. Operators running this headlessly can
        disable it; the URL is still in the returned `authorize_url`.
      client_factory: optional httpx.Client factory. Tests pass a
        stub.
    """
    cf = client_factory or _default_client_factory
    with cf() as client:
        info = _get_authorize_url(bot_public_url, client)
        if not info.get("configured"):
            raise OAuthError(
                f"bot refused to issue an authorize URL: {info.get('reason')!r}"
            )
        url = info["url"]
        state = info["state"]
        if open_browser:
            try:
                opened = webbrowser.open(url, new=2, autoraise=True)
            except Exception as e:  # noqa: BLE001
                log.warning("oauth.browser_open_failed", error=str(e))
                opened = False
            if not opened:
                log.warning("oauth.browser_open_noop")
        status = _poll_fyers_status(bot_public_url, client, timeout_s=timeout_s)
        return OAuthOutcome(
            ok=True,
            public_url=bot_public_url,
            authorize_url=url,
            state=state,
            account_id=status.get("account_id"),
            app_id=status.get("app_id"),
        )


# ---------------------------------------------------------------------------
# Test seam: let tests inject a fake `webbrowser.open` and a fake
# `httpx.Client` without monkey-patching the module globals.
# ---------------------------------------------------------------------------


def _install_test_hooks(
    *,
    browser_open: Optional[Any] = None,
    client_factory: Optional[_ClientFactory] = None,
) -> Any:
    """Returns a context manager that restores both hooks on exit."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        nonlocal browser_open, client_factory
        orig_open = webbrowser.open
        orig_factory = _default_client_factory
        if browser_open is not None:
            webbrowser.open = browser_open  # type: ignore[assignment]
        if client_factory is not None:
            globals()["_default_client_factory"] = client_factory
        try:
            yield
        finally:
            webbrowser.open = orig_open  # type: ignore[assignment]
            globals()["_default_client_factory"] = orig_factory

    return _ctx()
