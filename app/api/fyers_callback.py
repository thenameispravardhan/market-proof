"""Fyers OAuth callback endpoint.

GET /api/fyers/callback?auth_code=<code>&state=<state>

Validates `state` (one-shot CSRF nonce), then exchanges the
`auth_code` for an access_token via Fyers' `validate-authcode`
endpoint. The token is stored on the `broker_accounts` row whose
`app_id` matches the FYERS_APP_ID env var. (For multi-account
operators, we look up by app_id first, then by redirect_uri.)

The access_token is NEVER logged. The `set_access_token` log line
in `FyersClient` only logs the count, not the value.

Errors:
  - 400 if `code` or `state` is missing.
  - 400 if `state` is not in the expected-states registry (CSRF).
  - 502 if the Fyers token exchange fails.
  - 404 if no matching broker_accounts row is found.

Returns a tiny HTML page on success so the operator can close the
tab. JSON is also returned under `application/json` accept.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.init import init_db
from app.db.models import AuditLog, BrokerAccount
from app.db.session import get_db
from app.execution.fyers_auth import (
    FyersAuthError,
    consume_state,
    exchange_code_for_token,
)
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/fyers", tags=["fyers"])

# Make sure the broker_accounts table exists.
init_db()


# The success page — plain HTML, no JS. Operator can close the tab.
_SUCCESS_HTML = """<!doctype html>
<html><head><title>Fyers auth complete</title></head>
<body style="font-family: sans-serif; max-width: 600px; margin: 4em auto;">
  <h1>Fyers auth complete</h1>
  <p>You can close this tab. The bot is now authorised for account
     <code>{account}</code>.</p>
</body></html>
"""


@router.get("/callback")
async def fyers_callback(
    request: Request,
    code: str = Query(..., alias="auth_code"),
    state: str = Query(...),
    db: Session = Depends(get_db),
) -> Any:
    """Fyers OAuth callback. Exchanges the code for a token, stores
    it on the matching broker_accounts row."""
    settings = get_settings()
    if not settings.FYERS_APP_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FYERS_APP_ID is not configured",
        )
    if not code or not isinstance(code, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing auth_code",
        )
    if not state or not isinstance(state, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing state",
        )
    # CSRF: one-shot state. We never log the value, only the
    # success/failure of the check.
    if not consume_state(state):
        log.warning("fyers.callback.bad_state")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired state",
        )
    # Find the matching broker_accounts row.
    app_id = settings.FYERS_APP_ID
    redirect_uri = settings.FYERS_REDIRECT_URI
    account: Optional[BrokerAccount] = db.execute(
        select(BrokerAccount).where(BrokerAccount.app_id == app_id)
    ).scalar_one_or_none()
    if account is None:
        # Fall back: prefer a REAL Fyers account (paper_mode=False)
        # whose app_id is set but mismatched (the common case is
        # the operator re-created their Fyers app on
        # https://myapi.fyers.in/dashboard/ — the old app_id was
        # deleted, they updated FYERS_APP_ID in .env, restarted
        # the backend, and re-ran OAuth; the existing row still
        # has the OLD app_id on it). We re-associate that row to
        # the new app_id below after a successful token exchange.
        # If multiple real Fyers accounts exist, we take the
        # first by id. We deliberately skip the default paper
        # `Fyers` row that the app seeds at startup — that row
        # is just a placeholder for paper-mode trading, not a
        # real Fyers integration, and re-associating it would
        # shadow the operator's actual real-account row.
        account = db.execute(
            select(BrokerAccount)
            .where(
                BrokerAccount.broker == "fyers",
                BrokerAccount.paper_mode == False,  # noqa: E712
                BrokerAccount.app_id.is_not(None),
            )
            .order_by(BrokerAccount.id.asc())
        ).scalars().first()
        if account is None:
            # Last resort: any Fyers row, including the seeded
            # paper placeholder. We only land here if the
            # operator has never had a real Fyers account row.
            account = db.execute(
                select(BrokerAccount)
                .where(BrokerAccount.broker == "fyers")
                .order_by(BrokerAccount.id.asc())
            ).scalars().first()
    if account is None:
        log.error("fyers.callback.no_account_for_app_id", app_id=app_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no broker_accounts row matches the configured FYERS_APP_ID",
        )
    # Track whether we're re-associating the row to a new app_id
    # so we can (a) update the row's app_id after a successful
    # token exchange, and (b) record both the old and new app_id
    # in the audit log for traceability.
    previous_app_id = account.app_id
    app_id_changed = previous_app_id != app_id
    # Exchange the code for a token.
    try:
        token = await exchange_code_for_token(
            app_id=app_id,
            secret_key=settings.FYERS_SECRET_KEY,
            code=code,
        )
    except FyersAuthError as e:
        log.error("fyers.callback.token_exchange_failed", error=str(e), status=e.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"fyers token exchange failed: {e}",
        )
    # Persist.
    account.access_token = token.access_token
    # Re-associate the row to the new app_id when the operator has
    # re-created their Fyers app. Without this, the trade page
    # uses the row's old app_id (cached from the previous OAuth
    # round) and Fyers rejects every place_order with
    # "Order placement restricted. Algo orders are not allowed
    # from this app <old-app-id>". The access_token and app_id
    # MUST be a matched pair; updating only one of them leaves
    # the row in an inconsistent state.
    if app_id_changed:
        log.warning(
            "fyers.callback.reassociated_app_id",
            account_id=account.id,
            previous_app_id=previous_app_id,
            new_app_id=app_id,
        )
        account.app_id = app_id
    # Also persist the redirect_uri for record-keeping if blank.
    if not account.redirect_uri:
        account.redirect_uri = redirect_uri
    # Write to audit_log. We deliberately do NOT log the token.
    # When the app_id changed we record both the old and the new
    # value in the audit row so an operator reviewing the log
    # later can see when the re-association happened.
    audit_after: dict[str, Any] = {"account_id": account.id, "app_id": app_id}
    audit_before: Optional[dict[str, Any]] = None
    if app_id_changed:
        audit_before = {"app_id": previous_app_id}
        audit_after["previous_app_id"] = previous_app_id
    db.add(
        AuditLog(
            actor="ui",
            action="fyers.token_rotated",
            target=f"broker_account:{account.id}",
            before=audit_before,
            after=audit_after,
        )
    )
    db.commit()

    log.info(
        "fyers.callback.success",
        account_id=account.id,
        account_name=account.name,
    )

    # The execution manager, if running, will pick up the new token
    # on its next signal (FyersLiveBackend is rebuilt lazily). We
    # also publish a settings-style event so any in-process manager
    # can drop its cached backend for this account immediately.
    from app.services.event_bus import event_bus
    try:
        await event_bus.publish(
            "broker.token_rotated",
            {"broker_account_id": int(account.id)},
        )
    except Exception:  # noqa: BLE001
        # Best-effort; the lazy rebuild also works.
        pass

    # Pick a response: HTML for browsers, JSON for API callers.
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept and "html" not in accept:
        return JSONResponse(
            {
                "ok": True,
                "account_id": account.id,
                "account_name": account.name,
            }
        )
    return HTMLResponse(_SUCCESS_HTML.format(account=account.name))


@router.get("/authorize-url")
async def fyers_authorize_url() -> dict[str, Any]:
    """One-shot helper: returns the Fyers authorize URL the operator
    should open in a browser. The `state` is registered for CSRF
    protection; the callback validates it.

    Shape matches what the "Connect Fyers" banner expects:
        {configured: bool, url: str, state: str, reason: str | None}
    When creds are missing we return `configured: false` with a clear
    reason (HTTP 200) so the UI can render the message instead of
    throwing on a 5xx.

    Pre-validation: we ping the OAuth URL with the configured
    `app_id` and inspect the response. Fyers returns either:
      * 200 with the login form HTML         → app is valid
      * 200 with a `<a href=".../error/...">Found</a>` tiny HTML
        page (or a 302 to that URL)         → app is invalid/deleted
    We surface that "deleted app" / "invalid clientId" case here
    so the operator sees the real reason in the bot UI *before*
    opening the popup, instead of having to read Fyers' error
    page after the OAuth flow silently dies.
    """
    import secrets

    settings = get_settings()
    if not settings.fyers_configured:
        return {
            "configured": False,
            "url": "",
            "state": "",
            "reason": (
                "FYERS_APP_ID / FYERS_SECRET_KEY are not set in .env. "
                "Add them and restart the backend."
            ),
        }
    from app.execution.fyers_auth import build_authorize_url, register_state

    state = secrets.token_urlsafe(24)
    register_state(state)
    url = build_authorize_url(
        app_id=settings.FYERS_APP_ID,
        redirect_uri=settings.FYERS_REDIRECT_URI,
        state=state,
    )
    # Pre-validate the configured app_id by pinging the OAuth URL.
    # Fyers responds 200 with the login form for a valid app, and
    # 200/302 to `.../api-login/error/index.html?error_msg=...` for
    # an invalid one. We let httpx follow the redirect (default)
    # so the final URL lands on either the login page or the error
    # page, and key off that. This is a best-effort validation —
    # Fyers can still 500 or change its behaviour; if anything goes
    # wrong here we silently fall through and return the URL.
    try:
        import httpx

        async with httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=True,
            headers={
                # Match the User-Agent we send to the Fyers data API
                # so the OAuth server doesn't see us as a script.
                "User-Agent": (
                    "Mozilla/5.0 (compatible; TradeBot/1.0; "
                    "+https://github.com/local/tradebot)"
                ),
            },
        ) as client:
            # Use the build_authorize_url output as the ping target.
            # We DON'T register `state` for the ping (already done
            # above) — this is a GET with no auth, just to see what
            # Fyers does with the app_id.
            resp = await client.get(url)
        final_url = str(resp.url)
        if "api-login/error" in final_url or "error_msg=" in final_url:
            # Fyers redirected to its own error page. Extract the
            # `error_msg` if present so the operator sees the
            # exact Fyers-side reason.
            from urllib.parse import parse_qs, urlparse

            qs = parse_qs(urlparse(final_url).query)
            err = (qs.get("error_msg") or ["invalid app_id"])[0]
            log.warning(
                "fyers.authorize_url.app_id_rejected",
                app_id=settings.FYERS_APP_ID, fyers_error=err,
            )
            return {
                "configured": False,
                "url": "",
                "state": "",
                "reason": (
                    f"Fyers rejected FYERS_APP_ID {settings.FYERS_APP_ID!r} "
                    f"as {err!r}. The app was likely deleted on "
                    "https://myapi.fyers.in/dashboard/. Create a new "
                    "Fyers app, then update FYERS_APP_ID and "
                    "FYERS_SECRET_KEY in .env and restart the backend."
                ),
            }
    except Exception as e:  # noqa: BLE001
        # Best-effort. Don't block the operator if Fyers is down or
        # the ping times out — let them try the real flow and let
        # the popup surface the error.
        log.info("fyers.authorize_url.precheck_skipped", error=str(e))

    return {"configured": True, "url": url, "state": state, "reason": None}


# -------------------------------------------------------------------------
# Status + disconnect — used by the UI "Connect Fyers" banner.
# -------------------------------------------------------------------------


@router.get("/status")
def fyers_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Report the high-level Fyers connection state for the UI.

    Drives the "Connect Fyers" banner. Three layers of state:

      1. `credentials_set`  — FYERS_APP_ID + FYERS_SECRET_KEY in .env
      2. `account_present`  — a Fyers broker_accounts row exists in the DB
      3. `connected`        — that row has a non-empty access_token

    The dashboard's "live data" status bar uses (3); the warning
    "FYERS NOT CONFIGURED" appears when (3) is false.
    """
    settings = get_settings()
    credentials_set = settings.fyers_configured
    # Look for a Fyers account whose app_id matches the configured one.
    # We deliberately require `app_id` to be set on the account row —
    # the seeded `Paper Account` (broker='fyers', app_id=None) is just
    # a placeholder for paper-mode trading, NOT a real Fyers integration.
    account: Optional[BrokerAccount] = None
    if credentials_set:
        account = db.execute(
            select(BrokerAccount)
            .where(
                BrokerAccount.broker == "fyers",
                BrokerAccount.app_id == settings.FYERS_APP_ID,
                BrokerAccount.app_id.is_not(None),
                BrokerAccount.enabled == True,  # noqa: E712
            )
            .order_by(BrokerAccount.id.asc())
        ).scalars().first()
    if account is None:
        account = db.execute(
            select(BrokerAccount)
            .where(
                BrokerAccount.broker == "fyers",
                BrokerAccount.app_id.is_not(None),
                BrokerAccount.enabled == True,  # noqa: E712
            )
            .order_by(BrokerAccount.id.asc())
        ).scalars().first()

    account_present = account is not None
    has_token = bool(account_present and account.access_token)  # type: ignore[union-attr]

    reason: Optional[str] = None
    if not credentials_set:
        reason = (
            "FYERS_APP_ID / FYERS_SECRET_KEY are not set in .env. "
            "Add them and restart the backend."
        )
    elif not account_present:
        reason = "Click 'Connect Fyers' to run the OAuth flow."
    elif not has_token:
        reason = (
            "Fyers account is configured but the access token is missing. "
            "Click 'Connect Fyers' to re-authorise (Fyers tokens expire daily)."
        )

    return {
        "connected": credentials_set and account_present and has_token,
        "credentials_set": credentials_set,
        "account_present": account_present,
        "app_id": settings.FYERS_APP_ID or None,
        # Surfaced so the Accounts "Fyers App Activation" card can show
        # the exact Redirect URL the operator must paste into the Fyers
        # dashboard — it has to match this value character-for-character
        # or OAuth fails with a redirect mismatch.
        "redirect_uri": settings.FYERS_REDIRECT_URI or None,
        "live_mode": settings.is_live,
        "reason": reason,
    }


@router.post("/disconnect")
async def fyers_disconnect(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Clear the Fyers access token (and rotate if possible).

    The credentials in .env stay put — this only kills the current
    OAuth session. The operator can re-authorise via the
    /authorize-url flow at any time.

    Use case: the access token has expired or you want to force a
    re-auth. After calling this, the dashboard's status bar will
    switch back to "FYERS NOT CONFIGURED" and the next signal will
    skip live routing (paper mode still works).
    """
    settings = get_settings()
    if not settings.fyers_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FYERS creds are not configured in .env",
        )

    account: Optional[BrokerAccount] = db.execute(
        select(BrokerAccount)
        .where(
            BrokerAccount.broker == "fyers",
            BrokerAccount.app_id == settings.FYERS_APP_ID,
            BrokerAccount.enabled == True,  # noqa: E712
        )
        .order_by(BrokerAccount.id.asc())
    ).scalars().first()
    if account is None:
        # The configured FYERS_APP_ID may not match the row's app_id:
        # the operator re-created their Fyers app and updated .env, but
        # the broker_accounts row still carries the OLD app_id (written
        # at the last OAuth). Disconnect must STILL find and clear that
        # row — otherwise the stale token lingers, order placement keeps
        # authenticating as the old app, and the banner is stuck on
        # "Connected". Fall back to the first enabled real Fyers account
        # that actually holds a token. (This was the "disconnect does
        # nothing" bug.)
        account = db.execute(
            select(BrokerAccount)
            .where(
                BrokerAccount.broker == "fyers",
                BrokerAccount.paper_mode == False,  # noqa: E712
                BrokerAccount.access_token.is_not(None),
                BrokerAccount.enabled == True,  # noqa: E712
            )
            .order_by(BrokerAccount.id.asc())
        ).scalars().first()
    if account is None:
        return {"ok": False, "reason": "no connected fyers account to disconnect"}

    account.access_token = None
    db.add(
        AuditLog(
            actor="user",
            action="fyers.disconnect",
            target=f"broker_account:{account.id}",
            before={"has_token": True},
            after={"has_token": False},
        )
    )
    db.commit()
    # Drop any cached FyersLiveBackend for this account so the next
    # order can't keep trading on the just-revoked token held in the
    # manager's in-memory backend cache.
    from app.services.event_bus import event_bus
    try:
        await event_bus.publish(
            "broker.token_rotated",
            {"broker_account_id": int(account.id)},
        )
    except Exception:  # noqa: BLE001
        pass
    log.info("fyers.disconnect", account_id=account.id, account_name=account.name)
    return {"ok": True}
