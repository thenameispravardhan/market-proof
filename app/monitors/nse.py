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

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from app.logging_config import get_logger
from app.monitors.base import BaseMonitor, RawAnnouncement

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


def _parse_nse_date(value: Any) -> Optional[datetime]:
    """Parse an NSE date string to a UTC datetime.

    Accepts None / "" / non-strings by returning None. Treats naive
    datetimes as IST (UTC+5:30) — NSE's wall clock — and converts to
    UTC. This matches what a Mumbai-based trader expects to see.
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Strip trailing timezone hint like " IST" if present.
    s = re.sub(r"\s+[A-Z]{2,4}$", "", s).strip()
    for fmt in _NSE_DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        # Naive datetimes: assume IST (UTC+5:30) since NSE publishes
        # in IST. We don't know timezone from the wire so this is the
        # standard interpretation.
        if dt.tzinfo is None:
            ist = timezone(timedelta(hours=5, minutes=30))
            dt = dt.replace(tzinfo=ist)
        return dt.astimezone(timezone.utc)
    return None


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
        data = json.loads(raw)
    except json.JSONDecodeError:
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
# Wire-level fetchers
#
# Two strategies are tried in order:
#   1. `fetch_nse_with_httpx` — fast path, no browser launch, uses
#      a 2-step cookie priming dance (GET / to get `nseappid`, then
#      GET the XHR with the same cookie jar).
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

    `transport` is an optional httpx transport (e.g. MockTransport
    for tests). When provided we skip the `http2=True` flag because
    the mock doesn't speak HTTP/2.

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

    client_kwargs: dict[str, Any] = {
        "headers": dict(NSE_REQUEST_HEADERS),
        "timeout": 15.0,
        "follow_redirects": True,
    }
    # NSE ships gzip-compressed responses; ask for identity encoding
    # so the response.text / response.content pipeline returns the
    # plain JSON we expect to parse.
    client_kwargs["headers"]["Accept-Encoding"] = "identity"
    if transport is not None:
        client_kwargs["transport"] = transport
    else:
        # NSE serves HTTP/2; httpx 0.28 supports it natively.
        client_kwargs["http2"] = True

    async with httpx.AsyncClient(**client_kwargs) as client:
        # Step 1: prime cookies. The home page is much smaller than the
        # corporate-filings landing and rarely returns HTTP/2 errors.
        # NSE's CDN returns 403 for many user-agents and IPs — we
        # treat 403 as a soft "cookies may be incomplete, try the
        # XHR anyway". The XHR has its own CORS check; if the cookies
        # are missing it'll return 401/403 and the Playwright path
        # will be tried.
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

    Uses `context.request` (Playwright's HTTP client) for the XHR
    call so the cookie jar is shared with the browser. Falls back
    to a 2nd in-page fetch() if `context.request` is blocked by
    NSE's CDN.

    Returns the raw JSON text. Raises `_RetryableError` on network /
    5xx / 429 / 4xx.
    """
    from app.monitors.base import _RetryableError

    try:
        from playwright.async_api import async_playwright
    except ImportError as e:  # pragma: no cover — env-specific
        raise _RetryableError(
            "playwright not installed; run `pip install playwright && playwright install chromium`"
        ) from e

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            raise _RetryableError(f"chromium launch failed: {e}") from e
        try:
            context = await browser.new_context(
                user_agent=NSE_REQUEST_HEADERS["User-Agent"],
                extra_http_headers={
                    "Accept-Language": NSE_REQUEST_HEADERS["Accept-Language"],
                },
            )
            # Step 1: hit a light URL to seed cookies.
            page = await context.new_page()
            try:
                response = await page.goto(
                    NSE_COOKIE_SEED_URL, wait_until="domcontentloaded", timeout=15000
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
            try:
                await browser.close()
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
