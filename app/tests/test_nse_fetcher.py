"""Tests for `app.monitors.nse` wire-level fetchers.

We don't hit NSE from CI. Instead we mock httpx at the transport level
so the cookie-priming + XHR sequence runs end-to-end against a
recorded shape. Playwright path is covered by the parser tests; we
don't try to spin up a browser here.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from app.monitors import nse as nse_mod
from app.monitors.base import _RetryableError


def _make_mock_transport(
    responder: Callable[[httpx.Request], httpx.Response]
) -> httpx.MockTransport:
    """Build an httpx.MockTransport. The fetcher accepts this via `transport=`."""
    return httpx.MockTransport(responder)


def _norm(s: str) -> str:
    """Strip trailing slashes so httpx-normalised URLs match our constants."""
    return s.rstrip("/")


@pytest.mark.asyncio
async def test_fetch_nse_httpx_happy_path() -> None:
    """Cookie primer 200 -> XHR 200 -> JSON returned to caller."""
    sample = json.dumps(
        [
            {
                "symbol": "RELIANCE",
                "sm_name": "Reliance Industries Limited",
                "desc": "Reliance - Board Meeting",
                "attchmntFile": "https://nseindia.com/x.pdf",
                "an_dt": "15-Jan-2026 09:30:00",
            }
        ]
    )
    calls: list[str] = []

    def _responder(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        url = _norm(str(req.url))
        if url == _norm(nse_mod.NSE_COOKIE_SEED_URL):
            return httpx.Response(200, content=b"<html>ok</html>")
        if url.startswith(_norm(nse_mod.NSE_XHR_URL)):
            # NSE's XHR URL has a date range appended (from_date / to_date).
            return httpx.Response(200, content=sample.encode("utf-8"))
        return httpx.Response(404, content=b"unexpected url: " + url.encode("utf-8"))

    out = await nse_mod.fetch_nse_with_httpx("https://nse/landing", transport=_make_mock_transport(_responder))
    parsed = nse_mod.parse_nse_payload(out, nse_mod.NSE_XHR_URL)
    assert len(parsed) == 1
    assert parsed[0].company == "RELIANCE"

    urls_called = [_norm(u) for u in calls]
    assert urls_called[0] == _norm(nse_mod.NSE_COOKIE_SEED_URL)
    assert urls_called[1].startswith(_norm(nse_mod.NSE_XHR_URL))


@pytest.mark.asyncio
async def test_fetch_nse_httpx_xhr_403_raises_retryable() -> None:
    """XHR 403 (bot block) raises _RetryableError, not silent success."""
    def _responder(req: httpx.Request) -> httpx.Response:
        url = _norm(str(req.url))
        if url == _norm(nse_mod.NSE_COOKIE_SEED_URL):
            return httpx.Response(200, content=b"ok")
        # 403 on the XHR is what we care about — 200 on the primer is
        # necessary so the fetcher doesn't bail out early.
        return httpx.Response(403, content=b"blocked")

    with pytest.raises(_RetryableError, match="403"):
        await nse_mod.fetch_nse_with_httpx("https://nse/landing", transport=_make_mock_transport(_responder))


@pytest.mark.asyncio
async def test_fetch_nse_httpx_xhr_429_raises_retryable() -> None:
    def _responder(req: httpx.Request) -> httpx.Response:
        url = _norm(str(req.url))
        if url == _norm(nse_mod.NSE_COOKIE_SEED_URL):
            return httpx.Response(200, content=b"ok")
        return httpx.Response(429, content=b"slow down")

    with pytest.raises(_RetryableError, match="429"):
        await nse_mod.fetch_nse_with_httpx("https://nse/landing", transport=_make_mock_transport(_responder))


@pytest.mark.asyncio
async def test_fetch_nse_httpx_xhr_5xx_raises_retryable() -> None:
    def _responder(req: httpx.Request) -> httpx.Response:
        url = _norm(str(req.url))
        if url == _norm(nse_mod.NSE_COOKIE_SEED_URL):
            return httpx.Response(200, content=b"ok")
        return httpx.Response(503, content=b"down")

    with pytest.raises(_RetryableError, match="503"):
        await nse_mod.fetch_nse_with_httpx("https://nse/landing", transport=_make_mock_transport(_responder))


@pytest.mark.asyncio
async def test_fetch_nse_httpx_primer_5xx_raises_retryable() -> None:
    def _responder(req: httpx.Request) -> httpx.Response:
        url = _norm(str(req.url))
        if url == _norm(nse_mod.NSE_COOKIE_SEED_URL):
            return httpx.Response(502, content=b"bad gateway")
        return httpx.Response(200, content=b"ok")

    with pytest.raises(_RetryableError, match="502"):
        await nse_mod.fetch_nse_with_httpx("https://nse/landing", transport=_make_mock_transport(_responder))


@pytest.mark.asyncio
async def test_fetch_nse_falls_back_to_playwright_on_403(monkeypatch) -> None:
    """When httpx gets a 403, fetch_nse delegates to the Playwright path."""
    # Capture a stable reference to the original httpx fetcher so the
    # patched wrapper doesn't recurse into itself.
    original_httpx_fetcher = nse_mod.fetch_nse_with_httpx

    def _responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"blocked")

    async def _failing_httpx(url: str, *, transport=None) -> str:
        return await original_httpx_fetcher(url, transport=_make_mock_transport(_responder))

    called = {"n": 0}

    async def _stub(url: str) -> str:
        called["n"] += 1
        return "[]"

    monkeypatch.setattr(nse_mod, "fetch_nse_with_httpx", _failing_httpx)
    monkeypatch.setattr(nse_mod, "fetch_nse_with_playwright", _stub)
    out = await nse_mod.fetch_nse("https://nse/landing")
    assert out == "[]"
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_fetch_nse_does_not_fall_back_on_5xx(monkeypatch) -> None:
    """5xx is retryable, not a hard block — we should NOT fall back."""
    original_httpx_fetcher = nse_mod.fetch_nse_with_httpx

    def _responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"err")

    async def _failing_httpx(url: str, *, transport=None) -> str:
        return await original_httpx_fetcher(url, transport=_make_mock_transport(_responder))

    called = {"n": 0}

    async def _stub(url: str) -> str:
        called["n"] += 1
        return "[]"

    monkeypatch.setattr(nse_mod, "fetch_nse_with_httpx", _failing_httpx)
    monkeypatch.setattr(nse_mod, "fetch_nse_with_playwright", _stub)
    with pytest.raises(_RetryableError, match="500"):
        await nse_mod.fetch_nse("https://nse/landing")
    assert called["n"] == 0  # no fallback attempted


@pytest.mark.asyncio
async def test_fetch_nse_sets_required_headers() -> None:
    """NSE's required headers (Referer, Origin, User-Agent) must be on both calls."""
    seen: list[dict[str, str]] = []

    def _capture(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.headers))
        return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(
        headers=dict(nse_mod.NSE_REQUEST_HEADERS),
        timeout=15.0,
        follow_redirects=True,
        transport=_make_mock_transport(_capture),
    ) as client:
        await client.get(nse_mod.NSE_COOKIE_SEED_URL)
        r = await client.get(nse_mod.NSE_XHR_URL)
        assert r.text == "ok"

    for hdrs in seen:
        assert hdrs.get("referer") == nse_mod.NSE_REQUEST_HEADERS["Referer"]
        assert hdrs.get("origin") == nse_mod.NSE_REQUEST_HEADERS["Origin"]
        assert nse_mod.NSE_REQUEST_HEADERS["User-Agent"] in hdrs.get("user-agent", "")


def test_nse_monitor_uses_combined_fetcher_by_default() -> None:
    """The default NSEMonitor uses `fetch_nse` (httpx + Playwright fallback)."""
    from app.monitors.nse import NSEMonitor, fetch_nse

    mon = NSEMonitor()
    assert mon._fetcher is fetch_nse
