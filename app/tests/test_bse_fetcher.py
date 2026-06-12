"""Tests for `app.monitors.bse` wire-level fetchers.

We don't hit BSE from CI. Instead we mock httpx at the transport
level so the cookie-priming + API sequence runs end-to-end against
a recorded shape.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
import pytest

from app.monitors import bse as bse_mod
from app.monitors.base import _RetryableError


def _make_mock_transport(
    responder: Callable[[httpx.Request], httpx.Response]
) -> httpx.MockTransport:
    return httpx.MockTransport(responder)


def _norm(s: str) -> str:
    return s.rstrip("/")


@pytest.mark.asyncio
async def test_fetch_bse_httpx_happy_path() -> None:
    sample = json.dumps(
        {
            "Table": [
                {
                    "SCRIP_CD": 500325,
                    "HEADLINE": "Board Meeting",
                    "NEWS_DT": "15 Jan 2026 09:30:00",
                    "ATTACHMENTNAME": "https://www.bseindia.com/x.pdf",
                }
            ]
        }
    )
    calls: list[str] = []

    def _responder(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        url = str(req.url)
        if _norm(url) == _norm(bse_mod.BSE_COOKIE_SEED_URL):
            return httpx.Response(200, content=b"<html>ok</html>")
        if "BseIndiaAPI" in url:
            return httpx.Response(200, content=sample.encode("utf-8"))
        return httpx.Response(404, content=b"unexpected url: " + url.encode("utf-8"))

    out = await bse_mod.fetch_bse_with_httpx("https://bse/landing", transport=_make_mock_transport(_responder))
    # The fetcher fans out across BSE_CATEGORIES and merges. The
    # mocked transport returns the same single-row payload for every
    # category, so the merged payload has one row per category.
    # Dedup happens later in the monitor (DB unique content_hash), so
    # the parser sees 8 rows.
    parsed = bse_mod.parse_bse_payload(out, bse_mod.BSE_API_URL)
    assert len(parsed) == len(bse_mod.BSE_CATEGORIES)
    assert all(p.company == "500325" and p.title == "Board Meeting" for p in parsed)

    # Cookie primer called first, then the BSE API at least once.
    assert _norm(calls[0]) == _norm(bse_mod.BSE_COOKIE_SEED_URL)
    assert any("BseIndiaAPI" in c for c in calls[1:])


@pytest.mark.asyncio
async def test_fetch_bse_httpx_api_403_raises_retryable() -> None:
    def _responder(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if _norm(url) == _norm(bse_mod.BSE_COOKIE_SEED_URL):
            return httpx.Response(200, content=b"ok")
        return httpx.Response(403, content=b"blocked")

    with pytest.raises(_RetryableError, match="403"):
        await bse_mod.fetch_bse_with_httpx("https://bse/landing", transport=_make_mock_transport(_responder))


@pytest.mark.asyncio
async def test_fetch_bse_httpx_api_429_raises_retryable() -> None:
    def _responder(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if _norm(url) == _norm(bse_mod.BSE_COOKIE_SEED_URL):
            return httpx.Response(200, content=b"ok")
        return httpx.Response(429, content=b"slow")

    with pytest.raises(_RetryableError, match="429"):
        await bse_mod.fetch_bse_with_httpx("https://bse/landing", transport=_make_mock_transport(_responder))


@pytest.mark.asyncio
async def test_fetch_bse_httpx_api_5xx_raises_retryable() -> None:
    def _responder(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if _norm(url) == _norm(bse_mod.BSE_COOKIE_SEED_URL):
            return httpx.Response(200, content=b"ok")
        return httpx.Response(503, content=b"down")

    with pytest.raises(_RetryableError, match="503"):
        await bse_mod.fetch_bse_with_httpx("https://bse/landing", transport=_make_mock_transport(_responder))


@pytest.mark.asyncio
async def test_fetch_bse_httpx_primer_5xx_raises_retryable() -> None:
    def _responder(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if _norm(url) == _norm(bse_mod.BSE_COOKIE_SEED_URL):
            return httpx.Response(502, content=b"err")
        return httpx.Response(200, content=b"ok")

    with pytest.raises(_RetryableError, match="502"):
        await bse_mod.fetch_bse_with_httpx("https://bse/landing", transport=_make_mock_transport(_responder))


@pytest.mark.asyncio
async def test_fetch_bse_falls_back_to_playwright_on_403(monkeypatch) -> None:
    original_httpx_fetcher = bse_mod.fetch_bse_with_httpx

    def _responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"blocked")

    async def _failing_httpx(url: str, *, transport=None) -> str:
        return await original_httpx_fetcher(url, transport=_make_mock_transport(_responder))

    called = {"n": 0}

    async def _stub(url: str) -> str:
        called["n"] += 1
        return "{}"

    monkeypatch.setattr(bse_mod, "fetch_bse_with_httpx", _failing_httpx)
    monkeypatch.setattr(bse_mod, "fetch_bse_with_playwright", _stub)
    out = await bse_mod.fetch_bse("https://bse/landing")
    assert out == "{}"
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_fetch_bse_does_not_fall_back_on_5xx(monkeypatch) -> None:
    original_httpx_fetcher = bse_mod.fetch_bse_with_httpx

    def _responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"err")

    async def _failing_httpx(url: str, *, transport=None) -> str:
        return await original_httpx_fetcher(url, transport=_make_mock_transport(_responder))

    called = {"n": 0}

    async def _stub(url: str) -> str:
        called["n"] += 1
        return "{}"

    monkeypatch.setattr(bse_mod, "fetch_bse_with_httpx", _failing_httpx)
    monkeypatch.setattr(bse_mod, "fetch_bse_with_playwright", _stub)
    with pytest.raises(_RetryableError, match="500"):
        await bse_mod.fetch_bse("https://bse/landing")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_fetch_bse_sets_required_headers() -> None:
    """BSE needs Referer set to the corporate-announcements page."""
    seen: list[dict[str, str]] = []

    def _capture(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.headers))
        return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(
        headers=dict(bse_mod.BSE_REQUEST_HEADERS),
        timeout=15.0,
        follow_redirects=True,
        transport=_make_mock_transport(_capture),
    ) as client:
        await client.get(bse_mod.BSE_COOKIE_SEED_URL)
        r = await client.get(bse_mod._bse_window_url("Company Update"))
        assert r.text == "ok"

    for hdrs in seen:
        assert hdrs.get("referer") == bse_mod.BSE_REQUEST_HEADERS["Referer"]
        assert hdrs.get("origin") == bse_mod.BSE_REQUEST_HEADERS["Origin"]


def test_bse_window_url_uses_rolling_dates() -> None:
    """The BSE window URL has strPrevDate + strToDate matching the rolling 2-day window."""
    url = bse_mod._bse_window_url("Company Update", lookback_days=2)
    assert "strPrevDate=" in url
    assert "strToDate=" in url
    assert "strCat=Company+Update" in url
    assert "strSearch=P" in url
    assert "strType=C" in url
    assert "pageno=1" in url
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(timezone.utc).astimezone(ist)
    for date_str in ("strPrevDate", "strToDate"):
        prefix = f"{date_str}="
        idx = url.find(prefix) + len(prefix)
        end = url.find("&", idx)
        assert end > idx
        d = url[idx:end]
        parsed = datetime.strptime(d, "%Y%m%d")
        # Within ±3 days of now (lookback + jitter).
        assert abs((parsed.date() - now_ist.date()).days) <= 3


def test_bse_monitor_uses_combined_fetcher_by_default() -> None:
    from app.monitors.bse import BSEMonitor, fetch_bse

    mon = BSEMonitor()
    assert mon._fetcher is fetch_bse
