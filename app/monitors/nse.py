"""NSE corporate announcements monitor.

NSE publishes corporate announcements on a single page that's largely
server-rendered, but the table is hydrated by an XHR call. We hit the
XHR endpoint for stability and a smaller payload, and we fall back to
the page itself if the XHR fails.

Endpoints (used in production by the Playwright fetcher):

  - https://www.nseindia.com/companies-listing/corporate-filings-announcements
  - https://www.nseindia.com/api/corporate-announcements?index=equities
    (the XHR the page hits; requires the cookies set by the landing
    page to be present on the request)

For the parser we work with the JSON shape NSE's XHR returns::

    [
      {
        "symbol": "RELIANCE",
        "sm_name": "Reliance Industries Limited",
        "desc": "Reliance Industries Limited - Board Meeting Intimation",
        "attchmntFile": "https://nseindia.com/.../XYZ.pdf",
        "fileSize": "123.4 KB",
        "an_dt": "15-Jan-2026 09:30:00"
      },
      ...
    ]

The parser is deliberately tolerant: missing fields are skipped, not
raised. Date strings are localised and may arrive in `dd-MMM-yyyy`
format (e.g. "15-Jan-2026") or `dd-MMM-yyyy HH:MM:SS`; we normalise to
UTC datetimes. If a row's `posted_at` is unparseable we drop it.
"""
from __future__ import annotations

import json as _stdlib_json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from app.logging_config import get_logger
from app.monitors.base import BaseMonitor, RawAnnouncement

# orjson is ~3-5× faster than stdlib json for exchange payloads (200-500
# rows). Fall back to stdlib if orjson isn't installed.
try:
    import orjson as _fast_json

    def _parse_json(raw: Union[str, bytes]) -> Any:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return _fast_json.loads(raw)

    def _json_dumps(obj: Any) -> str:
        return _fast_json.dumps(obj).decode("utf-8")
except ImportError:
    def _parse_json(raw: Union[str, bytes]) -> Any:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return _stdlib_json.loads(raw)

    def _json_dumps(obj: Any) -> str:
        return _stdlib_json.dumps(obj)

log = get_logger(__name__)

# Public landing page — useful for the human-readable "source" field and
# for the parser fallback path.
NSE_LANDING_URL = (
    "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
)
# Lighter URL used solely to seed cookies. NSE's main landing page can
# return HTTP/2 protocol errors from some IPs; this one is much smaller
# and more reliable as a cookie primer.
NSE_COOKIE_SEED_URL = "https://www.nseindia.com/"
# XHR endpoint the page hits. Cookies set by visiting the seed URL
# must be present; the fetcher passes the same cookie jar.
# We use the ``from_date`` / ``to_date`` variant (dd-mm-yyyy) which
# returns the full announcement history for the window — up to a few
# hundred rows. NSE's "no params" endpoint only returns 20 most-recent.
NSE_XHR_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"

# Headers that the in-page fetch() sets. NSE rejects requests without
# these.
NSE_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": NSE_LANDING_URL,
    "Origin": "https://www.nseindia.com",
    "Connection": "keep-alive",
}

# Date formats we accept. Order matters: longer / more specific first.
_NSE_DATE_FORMATS: tuple[str, ...] = (
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%d-%b-%Y",
    "%d-%b-%y %H:%M:%S",
    "%d-%b-%y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


# Cache the last matched format so we skip the try/except loop for the
# common case (rows arrive in the same format 99.9% of the time).
_last_nse_date_fmt: str | None = None


def _parse_nse_date(value: Any) -> Optional[datetime]:
    """Parse an NSE date string to a UTC datetime.

    Accepts None / "" / non-strings by returning None. Treats naive
    datetimes as IST (UTC+5:30) — NSE's wall clock — and converts to
    UTC. This matches what a Mumbai-based trader expects to see.

    Optimization: caches the *winning* format after the first successful
    parse so subsequent rows skip the try/except loop entirely.
    """
    global _last_nse_date_fmt
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Strip trailing timezone hint like " IST" if present.
    s = re.sub(r"\s+[A-Z]{2,4}$", "", s).strip()
    # Fast path: try the last-winning format first (no exception).
    if _last_nse_date_fmt is not None:
        try:
            dt = datetime.strptime(s, _last_nse_date_fmt)
            return _to_utc(dt)
        except ValueError:
            pass  # fall through to full scan
    for fmt in _NSE_DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        _last_nse_date_fmt = fmt
        return _to_utc(dt)
    return None


def _to_utc(dt: datetime) -> datetime:
    """Normalise a naive-or-aware datetime to UTC (assume IST if naive)."""
    if dt.tzinfo is None:
        ist = timezone(timedelta(hours=5, minutes=30))
        dt = dt.replace(tzinfo=ist)
    return dt.astimezone(timezone.utc)


def _row_to_raw(row: dict[str, Any]) -> Optional[RawAnnouncement]:
    """Convert one NSE JSON row to a `RawAnnouncement`, or None to skip."""
    # NSE uses `symbol` for the ticker and `sm_name` for the company
    # name. We treat `symbol` as the canonical company identifier (it
    # is the column the bot trades on).
    symbol = (row.get("symbol") or "").strip()
    if not symbol:
        return None
    # Live API returns `attchmntText` (long human-readable summary) and
    # `desc` (a short category tag). For the bot's signal pipeline the
    # long summary is more useful; fall back to desc if missing.
    title = (
        row.get("attchmntText")
        or row.get("desc")
        or row.get("headline")
        or ""
    ).strip()
    if not title:
        return None
    posted_at = _parse_nse_date(row.get("an_dt") or row.get("dt"))
    if posted_at is None:
        return None
    # The live API exposes `attchmntFile` as a full URL. Older
    # responses used a bare path; we tolerate both.
    pdf_url = row.get("attchmntFile") or row.get("file_link") or None
    if isinstance(pdf_url, str):
        pdf_url = pdf_url.strip() or None
    return RawAnnouncement(
        company=symbol,
        title=title,
        posted_at=posted_at,
        pdf_url=pdf_url,
    )


def _nse_xhr_url(lookback_days: int = 1) -> str:
    """Build the NSE XHR URL with a date window.

    NSE's date params are ``dd-mm-yyyy``. With a 1-day window we get
    the last 24h of filings — usually 200-500 rows on a weekday. A
    wider window pulls more history but takes longer to ship.
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(timezone.utc).astimezone(ist)
    start = today - timedelta(days=lookback_days)
    fmt = "%d-%m-%Y"
    return (
        f"{NSE_XHR_URL}&from_date={start.strftime(fmt)}"
        f"&to_date={today.strftime(fmt)}"
    )


def parse_nse_payload(
    raw: Union[str, bytes], source_url: str
) -> list[RawAnnouncement]:
    """Pure parser — works on the raw XHR JSON response.

    Accepts str or bytes; bytes are decoded as utf-8. The endpoint
    must return a JSON array; if it returns an object with a `data`
    key, we use that. Anything else returns an empty list (and the
    monitor loop will back off and retry).
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        data = _parse_json(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        # Some NSE endpoints wrap the array under "data".
        data = data.get("data", data.get("records", []))
    if not isinstance(data, list):
        return []
    out: list[RawAnnouncement] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        parsed = _row_to_raw(row)
        if parsed is not None:
            out.append(parsed)
    return out


# -------------------------------------------------------------------------
# Persistent httpx client — reused across poll ticks so TCP/SSL
# handshakes and connection warm-up happen once per process lifetime
# instead of every second. The client is lazily created and uses
# HTTP/2 (NSE's native protocol). A separate test-client factory
# avoids coupling tests on the shared singleton.
# -------------------------------------------------------------------------

_nse_client: "httpx.AsyncClient | None" = None


async def _get_nse_client(*, transport: Any = None) -> "httpx.AsyncClient":
    """Return a shared persistent httpx client for NSE fetches.

    When ``transport`` is provided (test mocks) a *new* throwaway
    client is returned every time so the test's mock transport is
    always bound correctly.  For production calls (transport=None)
    the module-level singleton is created once and reused forever.
    """
    import httpx

    global _nse_client
    if transport is not None:
        client_kwargs: dict[str, Any] = {
            "headers": dict(NSE_REQUEST_HEADERS),
            "timeout": 8.0,
            "follow_redirects": True,
            "transport": transport,
        }
        client_kwargs["headers"]["Accept-Encoding"] = "identity"
        return httpx.AsyncClient(**client_kwargs)

    if _nse_client is not None:
        return _nse_client

    client_kwargs = {
        "headers": dict(NSE_REQUEST_HEADERS),
        "timeout": 8.0,
        "follow_redirects": True,
        "http2": True,
    }
    client_kwargs["headers"]["Accept-Encoding"] = "identity"
    _nse_client = httpx.AsyncClient(**client_kwargs)
    return _nse_client


def _close_nse_client() -> None:
    """Test / teardown helper — closes the shared client."""
    global _nse_client
    if _nse_client is not None:
        import anyio
        try:
            anyio.from_thread.run(_nse_client.aclose)
        except Exception:  # noqa: BLE001
            pass
        _nse_client = None


# -------------------------------------------------------------------------
# Wire-level fetchers
#
# Two strategies are tried in order:
#   1. `fetch_nse_with_httpx` — fast path, no browser launch, uses
#      a 2-step cookie priming dance (GET / to get `nseappid`, then
#      GET the XHR with the same cookie jar).  The httpx **client**
#      is now persistent across ticks so TCP/SSL setup is a one-time
#      cost; only the cookie-primer GET runs per tick.
#   2. `fetch_nse_with_playwright` — slow path, launches Chromium to
#      seed cookies via the in-page fetch() and also shares the
#      cookie jar across the request.
#
# The httpx path fails clean with a 403 in many environments because
# the cookie jar is incomplete; the Playwright path is the durable
# fallback.
# -------------------------------------------------------------------------


async def fetch_nse_with_httpx(url: str, *, transport: Any = None) -> str:
    """Pure-httpx NSE fetcher.

    `url` is the landing URL; we ignore it and build the XHR URL
    ourselves. The cookie jar is shared between the primer and the
    XHR call so `nseappid` etc. flow through.

    Uses the **shared persistent client** so TCP / SSL / HTTP/2
    session setup happens exactly once per process lifetime, saving
    ~200-400ms per poll tick.

    `transport` is an optional httpx transport (e.g. MockTransport
    for tests). When provided we skip the shared client and create
    a fresh one (so the mock transport is always correctly bound).

    Returns the raw JSON text. Raises `_RetryableError` on network /
    5xx / 429 / 403. The monitor loop will back off and retry; if
    403s persist, the `fetch_nse_with_playwright` fallback is wired
    in the manager.
    """
    from app.monitors.base import _RetryableError

    try:
        import httpx
    except ImportError as e:  # pragma: no cover
        raise _RetryableError("httpx not installed") from e

    client = await _get_nse_client(transport=transport)

    # Step 1: prime cookies. The home page is much smaller than the
    # corporate-filings landing and rarely returns HTTP/2 errors.
    try:
        primer = await client.get(NSE_COOKIE_SEED_URL)
    except Exception as e:  # noqa: BLE001
        log.debug("nse cookie primer network error", error=str(e))
        primer = None
    if primer is not None and primer.status_code >= 500:
        raise _RetryableError(f"nse primer {primer.status_code}")
    if primer is not None and primer.status_code in (401, 403):
        log.debug("nse primer 403 — proceeding anyway, XHR has its own auth")

    # Step 2: hit the XHR with the primed cookies.
    xhr_url = _nse_xhr_url(lookback_days=1)
    try:
        resp = await client.get(xhr_url)
    except Exception as e:  # noqa: BLE001
        raise _RetryableError(f"nse xhr failed: {e}") from e
    if resp.status_code == 429 or resp.status_code >= 500:
        raise _RetryableError(f"nse xhr {resp.status_code}")
    if resp.status_code in (401, 403):
        raise _RetryableError(f"nse xhr {resp.status_code} (likely bot block)")
    if resp.status_code >= 400:
        raise _RetryableError(f"nse xhr {resp.status_code}")
    return resp.text


async def fetch_nse_with_playwright(url: str) -> str:
    """Open Chromium, seed cookies via a small page, hit the XHR.

    Uses a persistent warm browser shared across NSE/BSE monitors so
    the fallback path doesn't pay a 1-2s cold-start Chromium penalty
    every time the httpx fast path gets blocked.

    Uses `context.request` (Playwright's HTTP client) for the XHR
    call so the cookie jar is shared with the browser. Falls back
    to a 2nd in-page fetch() if `context.request` is blocked by
    NSE's CDN.

    Returns the raw JSON text. Raises `_RetryableError` on network /
    5xx / 429 / 4xx.
    """
    from app.monitors.base import _RetryableError
    from app.monitors.browser_pool import get_browser

    try:
        browser = await get_browser()
    except Exception as e:
        raise _RetryableError(f"chromium launch failed: {e}") from e

    context = await browser.new_context(
        user_agent=NSE_REQUEST_HEADERS["User-Agent"],
        extra_http_headers={
            "Accept-Language": NSE_REQUEST_HEADERS["Accept-Language"],
        },
    )
    try:
        # Step 1: hit a light URL to seed cookies.
        page = await context.new_page()
        try:
            response = await page.goto(
                NSE_COOKIE_SEED_URL, wait_until="domcontentloaded", timeout=8000
            )
        except Exception as e:  # noqa: BLE001
            raise _RetryableError(f"nse seed goto failed: {e}") from e
        if response is not None and response.status >= 500:
            raise _RetryableError(f"nse seed {response.status}")
        if response is not None and response.status == 429:
            raise _RetryableError("nse seed 429")

        # Step 2: hit the XHR via context.request so cookies flow.
        xhr_url = _nse_xhr_url(lookback_days=1)
        try:
            api_resp = await context.request.get(
                xhr_url,
                headers={
                    "Accept": NSE_REQUEST_HEADERS["Accept"],
                    "Referer": NSE_REQUEST_HEADERS["Referer"],
                    "Origin": NSE_REQUEST_HEADERS["Origin"],
                },
            )
        except Exception as e:  # noqa: BLE001
            raise _RetryableError(f"nse xhr request failed: {e}") from e
        status = api_resp.status
        if status == 429 or status >= 500:
            raise _RetryableError(f"nse xhr {status}")
        if status >= 400:
            # 4xx other than 429 — usually means our IP / cookies
            # are blocked. Back off and retry.
            raise _RetryableError(f"nse xhr {status}")
        try:
            return await api_resp.text()
        except Exception as e:  # noqa: BLE001
            raise _RetryableError(f"nse xhr read failed: {e}") from e
    finally:
        # Close the context (cleans up pages/cookies) but NOT the
        # browser — it stays warm for the next monitor tick.
        try:
            await context.close()
        except Exception:  # noqa: BLE001
            pass


async def fetch_nse(url: str) -> str:
    """Try httpx first, fall back to Playwright on persistent 4xx.

    NSE's bot detection flips between strategies; this dual approach
    keeps the monitor running in more environments than a single
    path would.
    """
    from app.monitors.base import _RetryableError

    # First attempt: httpx (fast).
    try:
        return await fetch_nse_with_httpx(url)
    except _RetryableError as e:
        err = str(e)
        # 5xx, network, or 429 — worth retrying. 403 is a hard block;
        # the cookie jar is incomplete; try the Playwright path.
        if "403" in err or "401" in err or "bot block" in err or "IP block" in err:
            return await fetch_nse_with_playwright(url)
        # 5xx / network / 429 — bubble up so the monitor's backoff kicks in.
        raise


class NSEMonitor(BaseMonitor):
    """NSE announcement monitor."""

    exchange = "NSE"
    source_url = NSE_LANDING_URL

    def __init__(self, *, fetcher=None, parser=parse_nse_payload, **kwargs) -> None:
        if fetcher is None:
            fetcher = fetch_nse
        super().__init__(fetcher=fetcher, parser=parser, **kwargs)

