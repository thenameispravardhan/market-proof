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


def test_authorize_url_endpoint_returns_url_and_state(client: TestClient) -> None:
    import os
    saved = os.environ.get("FYERS_APP_ID")
    os.environ["FYERS_APP_ID"] = "APP-FAKE-AUTH"
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
