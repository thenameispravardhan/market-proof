"""Regression: turning Fyers trading OFF (the dashboard Fyers switch flips
`BrokerAccount.enabled`) must NOT cut the market-data feed or the OAuth
connection. Only order routing is gated on `enabled`; the realtime price
feed, quotes, history and option chain key on a CONNECTED account (one that
holds an OAuth token), independent of the trade switch.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.db.models import BrokerAccount


def _disabled_but_connected_fyers(db_session) -> BrokerAccount:
    """A real Fyers account with a token but trading switched OFF."""
    acc = BrokerAccount(
        name="Fyers", broker="fyers", app_id="APP-100", secret_key="sk",
        access_token="tok", paper_mode=False, enabled=False,
    )
    db_session.add(acc)
    db_session.commit()
    db_session.refresh(acc)
    return acc


def test_market_backend_served_when_trading_off(db_session, isolated_db):
    """`_fyers_backend()` (feeds the WS stream + indices + quotes + history)
    must still resolve a backend when the account is connected but trading
    is disabled — otherwise the realtime price feed disconnects."""
    from app.api.market import _fyers_backend

    _disabled_but_connected_fyers(db_session)
    assert _fyers_backend() is not None


def test_option_chain_account_served_when_trading_off(db_session, isolated_db):
    from app.api.options import _connected_fyers_account

    acc = _disabled_but_connected_fyers(db_session)
    found = _connected_fyers_account(db_session)
    assert found is not None and found.id == acc.id


def test_no_token_still_returns_none(db_session, isolated_db):
    """A disabled account with NO token isn't 'connected' → no data path."""
    from app.api.options import _connected_fyers_account

    db_session.add(BrokerAccount(
        name="Fyers", broker="fyers", app_id="APP-100", secret_key="sk",
        access_token=None, paper_mode=False, enabled=False,
    ))
    db_session.commit()
    assert _connected_fyers_account(db_session) is None


def test_trading_still_blocked_when_disabled(db_session, isolated_db):
    """The trade switch still works: a manual order on a disabled account is
    rejected (400). Decoupling data from `enabled` must not open a trade
    hole."""
    from app.api.orders import _require_real_account

    acc = _disabled_but_connected_fyers(db_session)
    with pytest.raises(HTTPException) as ei:
        _require_real_account(db_session, acc.id)
    assert ei.value.status_code == 400
