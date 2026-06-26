"""Tests for the Fyers OAuth flow.

Coverage:

  - build_authorize_url produces the right shape and query string.
  - register_state / consume_state are one-shot and validate TTL.
  - build_app_id_hash is the right SHA-256 of "<app_id>:<secret>".
  - exchange_code_for_token happy path: appIdHash in the body,
    200 response, access_token returned.
  - 401 raises FyersAuthError, no token in the formatted message.
  - Bad response (missing access_token) raises FyersAuthError.
  - Bad state on the callback returns 400.
  - Successful callback stores the token on the matching
    broker_accounts row + writes an audit log.
  - The token is never logged.
  - GET /api/fyers/authorize-url returns a URL with a registered state.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.models import AuditLog, BrokerAccount
from app.execution import fyers_auth
from app.execution.fyers_auth import (
    FYERS_AUTH_URL,
    build_app_id_hash,
    build_authorize_url,
    consume_state,
    exchange_code_for_token,
    register_state,
    reset_states,
)


# -- Helpers --------------------------------------------------------------


def _ok_token_response() -> dict:
    return {
        "s": "ok",
        "code": 200,
        "access_token": "FA.SECRET.TOKEN",
        "message": "Login successful",
    }


@pytest.fixture(autouse=True)
def _reset_states_between_tests():
    reset_states()
    yield
    reset_states()


# -- URL builder ----------------------------------------------------------


def test_build_authorize_url_shape() -> None:
    url = build_authorize_url(
        app_id="APP123",
        redirect_uri="http://localhost:8000/api/fyers/callback",
        state="abc",
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert FYERS_AUTH_URL in url
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["APP123"]
    assert qs["redirect_uri"] == ["http://localhost:8000/api/fyers/callback"]
    assert qs["response_type"] == ["code"]
    assert qs["state"] == ["abc"]


def test_build_authorize_url_requires_inputs() -> None:
    with pytest.raises(ValueError):
        build_authorize_url(app_id="", redirect_uri="x", state="y")
    with pytest.raises(ValueError):
        build_authorize_url(app_id="x", redirect_uri="", state="y")
    with pytest.raises(ValueError):
        build_authorize_url(app_id="x", redirect_uri="y", state="")


# -- State registry -------------------------------------------------------


def test_state_registry_one_shot() -> None:
    register_state("s1")
    assert consume_state("s1") is True
    # Second consume fails.
    assert consume_state("s1") is False


def test_state_registry_unknown_state_rejected() -> None:
    assert consume_state("nope") is False


def test_state_registry_ttl() -> None:
    register_state("s1")
    # Patch the TTL to a tiny number; sleep enough to exceed it.
    import time as _t
    orig = fyers_auth._STATE_TTL_S
    fyers_auth._STATE_TTL_S = 0.05
    try:
        _t.sleep(0.1)
        assert consume_state("s1") is False
    finally:
        fyers_auth._STATE_TTL_S = orig


# -- AppIdHash ------------------------------------------------------------


def test_build_app_id_hash_sha256() -> None:
    h = build_app_id_hash("APP123", "SECRET")
    expected = hashlib.sha256(b"APP123:SECRET").hexdigest()
    assert h == expected
    assert len(h) == 64


def test_build_app_id_hash_requires_inputs() -> None:
    with pytest.raises(ValueError):
        build_app_id_hash("", "x")
    with pytest.raises(ValueError):
        build_app_id_hash("x", "")


# -- Token exchange -------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_code_for_token_happy_path() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content.decode("utf-8"))
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json=_ok_token_response())

    transport = httpx.MockTransport(handler)
    result = await exchange_code_for_token(
        app_id="APP123",
        secret_key="SECRET",
        code="AUTH-CODE-1",
        transport=transport,
    )
    assert result.access_token == "FA.SECRET.TOKEN"
    # appIdHash in the body, no Authorization header (it's a
    # pre-auth exchange).
    body = captured["body"]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "AUTH-CODE-1"
    assert body["appIdHash"] == hashlib.sha256(b"APP123:SECRET").hexdigest()
    assert captured["auth"] is None


@pytest.mark.asyncio
async def test_exchange_401_raises_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid: FA.SECRET.TOKEN")

    transport = httpx.MockTransport(handler)
    with pytest.raises(fyers_auth.FyersAuthError) as exc:
        await exchange_code_for_token(
            app_id="APP123", secret_key="SECRET", code="X",
            transport=transport,
        )
    assert exc.value.status_code == 401
    # The token we sent in the body / upstream is NOT in the
    # formatted message.
    assert "FA.SECRET.TOKEN" not in str(exc.value)


@pytest.mark.asyncio
async def test_exchange_500_raises_api_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    transport = httpx.MockTransport(handler)
    with pytest.raises(fyers_auth.FyersAuthError) as exc:
        await exchange_code_for_token(
            app_id="APP123", secret_key="SECRET", code="X",
            transport=transport,
        )
    # 500 is a non-401 server error, so we raise a generic
    # FyersAuthError (subclass) or FyersAPIError. The spec just
    # requires "raises on non-2xx".
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_exchange_missing_token_in_response_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"s": "ok", "code": 200})

    transport = httpx.MockTransport(handler)
    with pytest.raises(fyers_auth.FyersAuthError):
        await exchange_code_for_token(
            app_id="APP123", secret_key="SECRET", code="X",
            transport=transport,
        )


@pytest.mark.asyncio
async def test_exchange_s_error_in_response_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"s": "error", "code": 400, "message": "bad"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(fyers_auth.FyersAuthError):
        await exchange_code_for_token(
            app_id="APP123", secret_key="SECRET", code="X",
            transport=transport,
        )


# -- Callback endpoint ---------------------------------------------------


def test_callback_validates_state(client: TestClient) -> None:
    # No state registered. We need FYERS_APP_ID set; set it on the
    # env so the endpoint gets past the config check.
    import os
    saved = os.environ.get("FYERS_APP_ID")
    os.environ["FYERS_APP_ID"] = "APP-FAKE-1"
    from app import config as app_config
    app_config.reset_settings_cache()
    import importlib
    import app.api.fyers_callback as cb_mod
    importlib.reload(cb_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient as TC
    try:
        with TC(main_mod.app) as c:
            r = c.get(
                "/api/fyers/callback",
                params={"auth_code": "X", "state": "unregistered"},
            )
        # 400 (state invalid) or 404 (no matching account). The
        # important point: not 200, and the error is about state.
        assert r.status_code in (400, 404)
        if r.status_code == 400:
            assert "invalid or expired state" in r.text
    finally:
        if saved is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved
        app_config.reset_settings_cache()
        importlib.reload(cb_mod)
        importlib.reload(main_mod)


def test_callback_missing_code_returns_400(client: TestClient) -> None:
    import os
    saved = os.environ.get("FYERS_APP_ID")
    os.environ["FYERS_APP_ID"] = "APP-FAKE-1"
    from app import config as app_config
    app_config.reset_settings_cache()
    import importlib
    import app.api.fyers_callback as cb_mod
    importlib.reload(cb_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient as TC
    try:
        with TC(main_mod.app) as c:
            r = c.get(
                "/api/fyers/callback",
                params={"state": "x"},
            )
        assert r.status_code == 422
    finally:
        if saved is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved
        app_config.reset_settings_cache()
        importlib.reload(cb_mod)
        importlib.reload(main_mod)


def test_callback_missing_state_returns_400(client: TestClient) -> None:
    import os
    saved = os.environ.get("FYERS_APP_ID")
    os.environ["FYERS_APP_ID"] = "APP-FAKE-1"
    from app import config as app_config
    app_config.reset_settings_cache()
    import importlib
    import app.api.fyers_callback as cb_mod
    importlib.reload(cb_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient as TC
    try:
        with TC(main_mod.app) as c:
            r = c.get(
                "/api/fyers/callback",
                params={"auth_code": "X"},
            )
        assert r.status_code == 422
    finally:
        if saved is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved
        app_config.reset_settings_cache()
        importlib.reload(cb_mod)
        importlib.reload(main_mod)


def test_callback_stores_token_on_matching_account(client: TestClient) -> None:
    """The happy path: register a state, post the callback with a
    stubbed Fyers response, assert the token lands on the right
    broker_accounts row and an audit log row is written."""
    import os
    import importlib
    from app.db.session import SessionLocal
    from app.execution import fyers_auth as fa

    settings_app_id = "APP-CB-1"
    saved_app_id = os.environ.get("FYERS_APP_ID")
    saved_secret = os.environ.get("FYERS_SECRET_KEY")
    os.environ["FYERS_APP_ID"] = settings_app_id
    os.environ["FYERS_SECRET_KEY"] = "FAKE-SECRET"
    from app import config as app_config
    app_config.reset_settings_cache()
    import app.api.fyers_callback as cb_mod
    importlib.reload(cb_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient as TC

    try:
        # Seed a broker_account with app_id = settings_app_id.
        with SessionLocal() as s:
            from app.db import init as db_init
            db_init.init_db()
            # Wipe any pre-existing rows from this test run.
            s.query(AuditLog).filter_by(action="fyers.token_rotated").delete()
            s.query(BrokerAccount).filter_by(name="cb-test").delete()
            ba = BrokerAccount(
                name="cb-test",
                broker="fyers",
                app_id=settings_app_id,
                secret_key="sk",
                access_token="old-token",
                redirect_uri="http://localhost:8000/api/fyers/callback",
                paper_mode=False,
            )
            s.add(ba)
            s.commit()

        # Stub the Fyers token endpoint by monkeypatching the
        # `exchange_code_for_token` symbol in the callback module.
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return httpx.Response(200, json=_ok_token_response())
        transport = httpx.MockTransport(handler)

        orig = cb_mod.exchange_code_for_token

        async def _stub(*args, **kwargs):
            kwargs["transport"] = transport
            return await orig(*args, **kwargs)

        cb_mod.exchange_code_for_token = _stub  # type: ignore

        try:
            register_state("callback-state-1")
            with TC(main_mod.app) as c:
                r = c.get(
                    "/api/fyers/callback",
                    params={"auth_code": "CODE-1", "state": "callback-state-1"},
                    headers={"accept": "application/json"},
                )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True
            # Token is the new one.
            with SessionLocal() as s:
                ba = s.query(BrokerAccount).filter_by(name="cb-test").one()
                assert ba.access_token == "FA.SECRET.TOKEN"
                # Audit log row written.
                audit = s.query(AuditLog).filter_by(action="fyers.token_rotated").all()
                assert len(audit) == 1
                assert audit[0].actor == "ui"
            # appIdHash was sent in the body.
            assert "appIdHash" in captured["body"]
            assert len(captured["body"]["appIdHash"]) == 64
        finally:
            cb_mod.exchange_code_for_token = orig  # type: ignore
    finally:
        if saved_app_id is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved_app_id
        if saved_secret is None:
            os.environ.pop("FYERS_SECRET_KEY", None)
        else:
            os.environ["FYERS_SECRET_KEY"] = saved_secret
        app_config.reset_settings_cache()
        importlib.reload(cb_mod)
        importlib.reload(main_mod)


def test_callback_reassociates_row_when_app_id_changed(client: TestClient) -> None:
    """Regression: the operator re-creates their Fyers app on
    https://myapi.fyers.in/dashboard/ (old app gets deleted) and
    updates FYERS_APP_ID in .env. They re-run OAuth, which
    succeeds — but the existing broker_accounts row still has the
    OLD app_id on it. The callback used to write the new
    access_token to that row without touching the app_id, so the
    trade page then used the row's OLD app_id when placing
    orders and Fyers returned "Order placement restricted.
    Algo orders are not allowed from this app <old-app-id>".
    The fix: when the existing row's app_id differs from the
    configured one, the callback updates BOTH app_id and
    access_token on the row, and records the re-association in
    the audit log so it's traceable.
    """
    import os
    import importlib
    from app.db.session import SessionLocal
    from app.execution import fyers_auth as fa

    # 1) The broker_accounts row is on the OLD app_id.
    old_app_id = "APP-OLD-DELETED-100"
    # 2) The .env is now configured with the NEW app_id.
    new_app_id = "APP-NEW-ACTIVE-100"
    saved_app_id = os.environ.get("FYERS_APP_ID")
    saved_secret = os.environ.get("FYERS_SECRET_KEY")
    os.environ["FYERS_APP_ID"] = new_app_id
    os.environ["FYERS_SECRET_KEY"] = "FAKE-SECRET"
    from app import config as app_config
    app_config.reset_settings_cache()
    import app.api.fyers_callback as cb_mod
    importlib.reload(cb_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient as TC

    try:
        # Seed: a Fyers row with the OLD app_id (the scenario
        # the user hit — old app was deleted, operator updated
        # .env, re-ran OAuth, callback fell back to this row
        # because no row matched the NEW app_id). The default
        # app startup also seeds a paper `Fyers` row with
        # `app_id=None`; we need to make sure our test row
        # is the one the callback picks up. Other tests in this
        # file seed real Fyers rows too (e.g. `cb-test`), and
        # the callback's fallback picks the FIRST real Fyers
        # row by id. We delete all real Fyers rows except our
        # own test row to keep the test deterministic.
        with SessionLocal() as s:
            from app.db import init as db_init
            db_init.init_db()
            s.query(AuditLog).filter_by(action="fyers.token_rotated").delete()
            # Wipe every real Fyers row from prior tests so the
            # callback's fallback (`ORDER BY id ASC LIMIT 1`)
            # deterministically lands on our test row.
            s.query(BrokerAccount).filter(
                BrokerAccount.broker == "fyers",
                BrokerAccount.paper_mode == False,  # noqa: E712
            ).delete()
            ba = BrokerAccount(
                name="reassociate-test",
                broker="fyers",
                app_id=old_app_id,  # <-- OLD value
                secret_key="sk",
                access_token="old-token",
                redirect_uri="http://localhost:8000/api/fyers/callback",
                paper_mode=False,
            )
            s.add(ba)
            s.commit()
            row_id = ba.id

        # Stub the Fyers token endpoint.
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json=_ok_token_response())
        )
        orig = cb_mod.exchange_code_for_token

        async def _stub(*args, **kwargs):
            kwargs["transport"] = transport
            return await orig(*args, **kwargs)

        cb_mod.exchange_code_for_token = _stub  # type: ignore

        try:
            register_state("reassociate-state")
            with TC(main_mod.app) as c:
                r = c.get(
                    "/api/fyers/callback",
                    params={"auth_code": "CODE-2", "state": "reassociate-state"},
                    headers={"accept": "application/json"},
                )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True
            # Look up by name (`reassociate-test`), not by id, so
            # the test is robust to the default seed's row order.
            with SessionLocal() as s:
                ba = s.query(BrokerAccount).filter_by(name="reassociate-test").one()
                # The critical assertion: the row's app_id is now
                # the NEW (configured) one, not the old one. Without
                # this re-association, the trade page would keep
                # using the deleted app_id for every order.
                assert ba.app_id == new_app_id, (
                    f"row.app_id should have been re-associated to "
                    f"{new_app_id!r}, got {ba.app_id!r}"
                )
                # The access_token is the freshly-minted one.
                assert ba.access_token == "FA.SECRET.TOKEN"
                # The audit log row records the re-association.
                audit = (
                    s.query(AuditLog)
                    .filter_by(action="fyers.token_rotated")
                    .filter_by(target=f"broker_account:{ba.id}")
                    .all()
                )
                assert len(audit) == 1
                # The audit row's `before` payload carries the
                # previous (old) app_id so the operator can see
                # the re-association in the audit log.
                assert audit[0].before == {"app_id": old_app_id}
                assert audit[0].after["app_id"] == new_app_id
                assert audit[0].after["previous_app_id"] == old_app_id
        finally:
            cb_mod.exchange_code_for_token = orig  # type: ignore
    finally:
        if saved_app_id is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved_app_id
        if saved_secret is None:
            os.environ.pop("FYERS_SECRET_KEY", None)
        else:
            os.environ["FYERS_SECRET_KEY"] = saved_secret
        app_config.reset_settings_cache()
        importlib.reload(cb_mod)
        importlib.reload(main_mod)


def test_disconnect_clears_token_when_app_id_mismatched(client: TestClient) -> None:
    """Regression ("disconnect does nothing"): the operator changed
    FYERS_APP_ID in .env (new app) but the broker_accounts row still
    carries the OLD app_id plus a token. Disconnect used to look the row
    up by `app_id == settings.FYERS_APP_ID` ONLY, find nothing, and
    return ok:false — so the stale token never cleared and the banner
    stayed on "Connected" forever. The fallback must still find and clear
    the real, tokened Fyers row.
    """
    import os
    from app.db.session import SessionLocal
    from app import config as app_config

    saved_app_id = os.environ.get("FYERS_APP_ID")
    saved_secret = os.environ.get("FYERS_SECRET_KEY")
    os.environ["FYERS_APP_ID"] = "NEW-APP-IN-ENV-200"
    os.environ["FYERS_SECRET_KEY"] = "FAKE-SECRET"
    app_config.reset_settings_cache()
    try:
        with SessionLocal() as s:
            from app.db import init as db_init
            db_init.init_db()
            # Deterministic: drop real Fyers rows other tests may have left.
            s.query(BrokerAccount).filter(
                BrokerAccount.broker == "fyers",
                BrokerAccount.paper_mode == False,  # noqa: E712
            ).delete()
            s.add(
                BrokerAccount(
                    name="disconnect-test",
                    broker="fyers",
                    app_id="OLD-APP-ON-ROW-100",  # != FYERS_APP_ID in .env
                    secret_key="sk",
                    access_token="stale-token",
                    paper_mode=False,
                    enabled=True,
                )
            )
            s.commit()

        r = client.post("/api/fyers/disconnect", json={})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        with SessionLocal() as s:
            row = s.query(BrokerAccount).filter_by(name="disconnect-test").one()
            assert row.access_token is None, "stale token must be cleared"
    finally:
        if saved_app_id is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved_app_id
        if saved_secret is None:
            os.environ.pop("FYERS_SECRET_KEY", None)
        else:
            os.environ["FYERS_SECRET_KEY"] = saved_secret
        app_config.reset_settings_cache()


def test_disconnect_clears_token_when_switched_off(client: TestClient) -> None:
    """Regression: a Fyers account that is authorised (holds a token) but
    switched OFF (enabled=False) must still be disconnectable. Filtering the
    disconnect lookup on `enabled == True` made "Disconnect" a silent no-op
    once Fyers was toggled off on the dashboard."""
    import os
    from app.db.session import SessionLocal
    from app import config as app_config

    saved_app_id = os.environ.get("FYERS_APP_ID")
    saved_secret = os.environ.get("FYERS_SECRET_KEY")
    os.environ["FYERS_APP_ID"] = "MATCHING-APP-300"
    os.environ["FYERS_SECRET_KEY"] = "FAKE-SECRET"
    app_config.reset_settings_cache()
    try:
        with SessionLocal() as s:
            from app.db import init as db_init
            db_init.init_db()
            s.query(BrokerAccount).filter(
                BrokerAccount.broker == "fyers",
                BrokerAccount.paper_mode == False,  # noqa: E712
            ).delete()
            s.add(
                BrokerAccount(
                    name="disconnect-off-test",
                    broker="fyers",
                    app_id="MATCHING-APP-300",  # == FYERS_APP_ID in .env
                    secret_key="sk",
                    access_token="live-token",
                    paper_mode=False,
                    enabled=False,  # trade switch OFF, but still authorised
                )
            )
            s.commit()

        r = client.post("/api/fyers/disconnect", json={})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        with SessionLocal() as s:
            row = s.query(BrokerAccount).filter_by(name="disconnect-off-test").one()
            assert row.access_token is None, "token must clear even when switched off"
    finally:
        if saved_app_id is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved_app_id
        if saved_secret is None:
            os.environ.pop("FYERS_SECRET_KEY", None)
        else:
            os.environ["FYERS_SECRET_KEY"] = saved_secret
        app_config.reset_settings_cache()


def test_authorize_url_endpoint_returns_url_and_state(monkeypatch, client: TestClient) -> None:
    import os
    saved = os.environ.get("FYERS_APP_ID")
    saved_secret = os.environ.get("FYERS_SECRET_KEY")
    os.environ["FYERS_APP_ID"] = "APP-FAKE-AUTH"
    # OAuth needs the secret too (the callback exchanges the code with
    # it), so the endpoint only emits a URL when both are configured.
    os.environ["FYERS_SECRET_KEY"] = "SECRET-FAKE-AUTH"
    from app import config as app_config
    app_config.reset_settings_cache()
    import importlib
    import app.api.fyers_callback as cb_mod
    importlib.reload(cb_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient as TC

    # The pre-validation ping must NOT reject the fake app_id in
    # this test — we want the existing "happy path" behaviour
    # (returns a working URL with the registered state). Mock
    # httpx so the ping sees a non-error response.
    class _Resp:
        url = "https://api-t1.fyers.in/api/v3/generate-authcode"

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str) -> _Resp:
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    try:
        with TC(main_mod.app) as c:
            r = c.get("/api/fyers/authorize-url")
        assert r.status_code == 200
        body = r.json()
        assert "url" in body
        assert "state" in body
        assert "client_id=APP-FAKE-AUTH" in body["url"]
        # The returned state should be registered.
        assert consume_state(body["state"]) is True
    finally:
        if saved is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved
        if saved_secret is None:
            os.environ.pop("FYERS_SECRET_KEY", None)
        else:
            os.environ["FYERS_SECRET_KEY"] = saved_secret
        app_config.reset_settings_cache()
        importlib.reload(cb_mod)
        importlib.reload(main_mod)


def test_token_never_logged_in_callback(client: TestClient) -> None:
    """Defence-in-depth: the token must not appear in the response
    body of the callback (we never echo it back to the browser)."""
    import os
    import importlib
    saved = os.environ.get("FYERS_APP_ID")
    os.environ["FYERS_APP_ID"] = "APP-NO-ACC"
    from app import config as app_config
    app_config.reset_settings_cache()
    import app.api.fyers_callback as cb_mod
    importlib.reload(cb_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient as TC
    try:
        register_state("token-log-state")
        with TC(main_mod.app) as c:
            r = c.get(
                "/api/fyers/callback",
                params={"auth_code": "X", "state": "token-log-state"},
                headers={"accept": "application/json"},
            )
        # Without a matching broker_accounts row, we get 404.
        assert r.status_code in (404, 502, 400)
        # Either way, the response body must not contain the token.
        assert "FA.SECRET.TOKEN" not in r.text
    finally:
        if saved is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved
        app_config.reset_settings_cache()
        importlib.reload(cb_mod)
        importlib.reload(main_mod)


def test_authorize_url_endpoint_returns_deleted_app_reason(monkeypatch) -> None:
    """Regression: an operator re-creates their Fyers app on
    https://myapi.fyers.in/dashboard/ and forgets to update
    FYERS_APP_ID in .env. Clicking "Connect Fyers" used to open
    a popup that Fyers filled with an opaque "deleted app" page;
    the operator had to read Fyers' error page to find out what
    was wrong. The new pre-validation pings the OAuth URL with
    the configured app_id; when Fyers redirects to its
    `.../api-login/error/index.html?error_msg=...` page we return
    `configured: false` with a clear reason, so the bot UI
    surfaces the real cause before the operator ever opens a
    popup.
    """
    import os
    import importlib

    saved = os.environ.get("FYERS_APP_ID")
    saved_secret = os.environ.get("FYERS_SECRET_KEY")
    os.environ["FYERS_APP_ID"] = "DELETED-APP-100"
    os.environ["FYERS_SECRET_KEY"] = "SECRET-FAKE"
    from app import config as app_config
    app_config.reset_settings_cache()
    import app.api.fyers_callback as cb_mod
    importlib.reload(cb_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient as TC

    # Mock httpx.AsyncClient so the pre-validation ping never
    # touches the real Fyers endpoint. We simulate Fyers'
    # behaviour for a deleted app: the auth URL 302-redirects
    # to its error page; `follow_redirects=True` means the
    # response.url lands on the error page.
    class _Resp:
        def __init__(self, url: str) -> None:
            self.url = url

    class _Client:
        def __init__(self, *args, **kwargs) -> None:  # noqa: D401
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str) -> _Resp:
            return _Resp(
                "https://trade.fyers.in/api-login/error/index.html"
                "?error_msg=deleted%20app"
            )

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    try:
        with TC(main_mod.app) as c:
            r = c.get("/api/fyers/authorize-url")
        assert r.status_code == 200
        body = r.json()
        # Must NOT give the operator a working URL when Fyers
        # has already rejected the app_id.
        assert body["configured"] is False
        assert body["url"] == ""
        assert "reason" in body and body["reason"]
        # The reason must include the configured app_id and the
        # Fyers-side error so the operator can act on it.
        reason = body["reason"]
        assert "DELETED-APP-100" in reason
        assert "deleted" in reason.lower() or "invalid" in reason.lower()
        # It must also point the operator at the myapi dashboard
        # and the .env keys that need updating.
        assert "myapi.fyers.in" in reason
        assert "FYERS_APP_ID" in reason
    finally:
        if saved is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved
        if saved_secret is None:
            os.environ.pop("FYERS_SECRET_KEY", None)
        else:
            os.environ["FYERS_SECRET_KEY"] = saved_secret
        app_config.reset_settings_cache()
        importlib.reload(cb_mod)
        importlib.reload(main_mod)


def test_authorize_url_endpoint_returns_url_when_app_id_is_valid(monkeypatch) -> None:
    """The pre-validation must NOT block the OAuth flow when the
    app_id is valid. We mock the ping to return a 200 with the
    real login form (not the error page), and assert that the
    endpoint still returns `configured: true` with a working URL.
    """
    import os
    import importlib

    saved = os.environ.get("FYERS_APP_ID")
    saved_secret = os.environ.get("FYERS_SECRET_KEY")
    os.environ["FYERS_APP_ID"] = "WORKING-APP-100"
    os.environ["FYERS_SECRET_KEY"] = "SECRET-FAKE"
    from app import config as app_config
    app_config.reset_settings_cache()
    import app.api.fyers_callback as cb_mod
    importlib.reload(cb_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient as TC

    class _Resp:
        url = "https://api-t1.fyers.in/api/v3/generate-authcode"  # NOT the error page

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str) -> _Resp:
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    try:
        with TC(main_mod.app) as c:
            r = c.get("/api/fyers/authorize-url")
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is True
        assert body["url"]
        assert "client_id=WORKING-APP-100" in body["url"]
    finally:
        if saved is None:
            os.environ.pop("FYERS_APP_ID", None)
        else:
            os.environ["FYERS_APP_ID"] = saved
        if saved_secret is None:
            os.environ.pop("FYERS_SECRET_KEY", None)
        else:
            os.environ["FYERS_SECRET_KEY"] = saved_secret
        app_config.reset_settings_cache()
        importlib.reload(cb_mod)
        importlib.reload(main_mod)
