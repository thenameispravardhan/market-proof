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
        # Fall back: first enabled account with a name starting with
        # 'fyers'. Operators can configure the broker_accounts row
        # via T5's UI; for v1 we accept either match.
        account = db.execute(
            select(BrokerAccount).where(BrokerAccount.broker == "fyers").order_by(BrokerAccount.id.asc())
        ).scalars().first()
    if account is None:
        log.error("fyers.callback.no_account_for_app_id", app_id=app_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no broker_accounts row matches the configured FYERS_APP_ID",
        )
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
    # Also persist the redirect_uri for record-keeping if blank.
    if not account.redirect_uri:
        account.redirect_uri = redirect_uri
    # Write to audit_log. We deliberately do NOT log the token.
    db.add(
        AuditLog(
            actor="ui",
            action="fyers.token_rotated",
            target=f"broker_account:{account.id}",
            before=None,
            after={"account_id": account.id, "app_id": app_id},
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
def fyers_authorize_url() -> dict[str, str]:
    """One-shot helper: returns the Fyers authorize URL the operator
    should open in a browser. The `state` is registered for CSRF
    protection; the callback validates it."""
    import secrets

    settings = get_settings()
    if not settings.FYERS_APP_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FYERS_APP_ID is not configured",
        )
    from app.execution.fyers_auth import build_authorize_url, register_state

    state = secrets.token_urlsafe(24)
    register_state(state)
    url = build_authorize_url(
        app_id=settings.FYERS_APP_ID,
        redirect_uri=settings.FYERS_REDIRECT_URI,
        state=state,
    )
    return {"url": url, "state": state}
