"""Pre-market Fyers self-test (app/services/fyers_selftest.py) and the
backup-age preflight check.

Every probe is injected, so nothing here touches the network or a broker.
"""
from __future__ import annotations

import base64
import json
import os
import time
from types import SimpleNamespace

import pytest

from app.services import fyers_selftest as ft
from app.services import health_report as hr


def _jwt(claims: dict) -> str:
    def enc(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'HS256'})}.{enc(claims)}.sig"


def _settings(monkeypatch, **over):
    base = dict(
        FYERS_APP_ID="XC12345-200", TRADING_MODE="live",
        FYERS_REQUIRED_APP_TYPE="200", FYERS_WHITELISTED_IPS="203.0.113.7",
        FYERS_EGRESS_IP_URL="", MARKET_CLOSE_IST="15:30",
    )
    base.update(over)
    monkeypatch.setattr(ft, "get_settings", lambda: SimpleNamespace(**base))


async def _ok_probe():
    return True, "authenticated as XY1234."


async def _ip(ip):
    async def probe():
        return ip
    return probe


def test_app_type_parses_the_app_id_suffix():
    assert ft.app_type_of("XC12345-200") == 200
    assert ft.app_type_of("XC12345-100") == 100
    assert ft.app_type_of("XC12345") is None
    assert ft.app_type_of("") is None


def test_type_100_app_is_an_error_live_and_a_warning_in_paper():
    assert ft.check_app_type("XC1-100", "200", live=True).status == ft.ERROR
    assert ft.check_app_type("XC1-100", "200", live=False).status == ft.WARN
    assert ft.check_app_type("XC1-200", "200", live=True).status == ft.OK
    assert ft.check_app_type("XC1-100", "", live=True).status == ft.SKIP
    assert ft.check_app_type("", "200", live=True).status == ft.ERROR


def test_egress_ip_outside_whitelist_is_an_error():
    assert ft.check_egress_ip("198.51.100.1", ["203.0.113.7"]).status == ft.ERROR
    assert ft.check_egress_ip("203.0.113.7", ["203.0.113.7"]).status == ft.OK
    assert ft.check_egress_ip("203.0.113.7", []).status == ft.SKIP
    assert ft.check_egress_ip(None, ["203.0.113.7"]).status == ft.WARN
    assert ft.parse_ip_list(" 1.2.3.4 ; 5.6.7.8,") == ["1.2.3.4", "5.6.7.8"]


@pytest.mark.asyncio
async def test_healthy_morning_has_no_problems(monkeypatch):
    _settings(monkeypatch)
    now = time.time()
    token = _jwt({"exp": now + 86400})
    result = await ft.run_fyers_selftest(
        account_lookup=lambda: (token, "XC12345-200"),
        profile_probe=_ok_probe,
        egress_probe=await _ip("203.0.113.7"),
        now_epoch=now,
    )
    assert result.ok and result.problems == []
    assert ft.last_result()["ok"] is True


@pytest.mark.asyncio
async def test_expired_token_and_wrong_ip_are_both_named(monkeypatch):
    _settings(monkeypatch)
    now = time.time()
    token = _jwt({"exp": now - 60})

    async def dead():
        return False, "authenticated call failed: fyers 401"

    result = await ft.run_fyers_selftest(
        account_lookup=lambda: (token, "XC12345-200"),
        profile_probe=dead,
        egress_probe=await _ip("198.51.100.1"),
        now_epoch=now,
    )
    names = {c.name for c in result.checks if c.status == ft.ERROR}
    assert names == {"token", "auth_probe", "egress_ip"}
    assert not result.ok


@pytest.mark.asyncio
async def test_missing_token_skips_the_auth_probe(monkeypatch):
    _settings(monkeypatch)
    called = []

    async def probe():
        called.append(1)
        return True, ""

    result = await ft.run_fyers_selftest(
        account_lookup=lambda: (None, None),
        profile_probe=probe,
        egress_probe=await _ip("203.0.113.7"),
    )
    assert called == []
    assert any(c.name == "token" and c.status == ft.ERROR for c in result.checks)


def test_token_minted_for_another_app_is_flagged():
    now = time.time()
    checks = ft.check_token(
        _jwt({"exp": now + 86400}), account_app_id="OLD-100",
        env_app_id="XC12345-200", now_epoch=now,
    )
    assert any(c.name == "token_app" and c.status == ft.ERROR for c in checks)


@pytest.mark.asyncio
async def test_preflight_includes_only_error_checks(monkeypatch):
    """Warnings stay on the banner; only errors page someone."""
    monkeypatch.setattr(
        hr, "get_settings",
        lambda: SimpleNamespace(AI_ANALYSIS_ENABLED=True, FYERS_SELFTEST_ENABLED=True),
    )
    monkeypatch.setattr(hr, "_resource_problems", lambda: [])
    monkeypatch.setattr(hr, "_db_integrity_problem", lambda: None)

    async def quote(_s):
        return {"last_price": 1.0}

    import app.api.market as market_mod
    monkeypatch.setattr(market_mod, "fetch_quote", quote)

    async def fake_selftest():
        r = ft.SelfTestResult(ran_at="x")
        r.checks = [
            ft.Check("app_type", ft.WARN, "type 100 in paper"),
            ft.Check("egress_ip", ft.ERROR, "not whitelisted"),
        ]
        return r

    monkeypatch.setattr(ft, "run_fyers_selftest", fake_selftest)
    problems = await hr.compile_preflight()
    assert problems == ["Fyers egress_ip: not whitelisted"]


# ---- backup age ---------------------------------------------------------


def _backup_env(monkeypatch, tmp_path, max_age=72.0):
    db = tmp_path / "trading.db"
    db.write_bytes(b"")
    monkeypatch.setattr(
        hr, "get_settings",
        lambda: SimpleNamespace(DATABASE_URL=f"sqlite:///{db}", BACKUP_MAX_AGE_HOURS=max_age),
    )
    d = tmp_path / "backups"
    d.mkdir()
    return d


def test_no_backup_at_all_is_a_problem(monkeypatch, tmp_path):
    _backup_env(monkeypatch, tmp_path)
    assert "No database backup" in hr._backup_problem()


def test_stale_backup_is_a_problem_and_fresh_one_is_not(monkeypatch, tmp_path):
    d = _backup_env(monkeypatch, tmp_path)
    f = d / "trading-20261001-183000.db"
    f.write_bytes(b"x")
    old = time.time() - 100 * 3600
    os.utime(f, (old, old))
    assert "100h old" in hr._backup_problem()
    os.utime(f, None)
    assert hr._backup_problem() is None


def test_failed_offsite_upload_is_a_problem(monkeypatch, tmp_path):
    d = _backup_env(monkeypatch, tmp_path)
    (d / "trading-20261001-183000.db").write_bytes(b"x")
    (d / "status.json").write_text(json.dumps({"offsite": "failed"}))
    assert "offsite backup upload FAILED" in hr._backup_problem()
    st = hr.backup_status()
    assert st["local_copies"] == 1 and st["offsite"] == "failed"


def test_backup_check_disabled_at_zero(monkeypatch, tmp_path):
    _backup_env(monkeypatch, tmp_path, max_age=0)
    assert hr._backup_problem() is None


def test_selftest_endpoint_reports_last_result(client):
    r = client.get("/api/system/fyers-selftest")
    assert r.status_code == 200
    assert "result" in r.json()
    r = client.get("/api/system/backups")
    assert r.status_code == 200
    assert r.json()["status"] is None   # in-memory test DB
