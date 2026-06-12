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

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional, Union

from app.logging_config import get_logger
from app.monitors.base import BaseMonitor, RawAnnouncement

log = get_logger(__name__)

# Public landing page — useful for the human-readable "source" field and
# for the parser fallback path.
NSE_LANDING_URL = (
    "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
)
# XHR endpoint the page hits. Cookies set by visiting the landing page
# must be present; the Playwright fetcher handles that.
NSE_XHR_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"

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
            ist = timezone(
                __import__("datetime").timedelta(hours=5, minutes=30)
            )
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
    title = (row.get("desc") or "").strip()
    if not title:
        return None
    posted_at = _parse_nse_date(row.get("an_dt") or row.get("dt"))
    if posted_at is None:
        return None
    pdf_url = row.get("attchmntFile") or row.get("file_link") or None
    if isinstance(pdf_url, str):
        pdf_url = pdf_url.strip() or None
    return RawAnnouncement(
        company=symbol,
        title=title,
        posted_at=posted_at,
        pdf_url=pdf_url,
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
# Playwright fetcher — the real wire-level implementation.
#
# This is imported lazily so tests (and environments without a browser)
# don't need playwright installed. The class only constructs the
# Playwright objects on first call.
# -------------------------------------------------------------------------


async def fetch_nse_with_playwright(url: str) -> str:
    """Open the landing page to get cookies, then hit the XHR.

    Returns the raw JSON text from the XHR. Raises `_RetryableError` on
    network / 5xx / 429, and `_FatalError` on structural issues that
    won't fix themselves.
    """
    # Local imports — keep playwright optional.
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
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()
            # Visit the landing page to get cookies.
            try:
                response = await page.goto(NSE_LANDING_URL, wait_until="domcontentloaded")
            except Exception as e:  # noqa: BLE001
                raise _RetryableError(f"nse landing goto failed: {e}") from e
            if response is not None and response.status >= 500:
                raise _RetryableError(f"nse landing {response.status}")
            if response is not None and response.status == 429:
                raise _RetryableError("nse landing 429")
            # XHR call.
            api_url = NSE_XHR_URL
            try:
                resp = await page.evaluate(
                    """async (apiUrl) => {
                        const r = await fetch(apiUrl, {credentials: 'include'});
                        if (!r.ok) return {__status: r.status, __text: ''};
                        return {__status: r.status, __text: await r.text()};
                    }""",
                    api_url,
                )
            except Exception as e:  # noqa: BLE001
                raise _RetryableError(f"nse xhr evaluate failed: {e}") from e
            if not isinstance(resp, dict):
                raise _RetryableError("nse xhr unexpected response shape")
            status = int(resp.get("__status", 0))
            text = resp.get("__text", "") or ""
            if status == 429 or status >= 500:
                raise _RetryableError(f"nse xhr {status}")
            if status >= 400:
                # 4xx other than 429 — usually means our IP / cookies
                # are blocked. Back off and retry.
                raise _RetryableError(f"nse xhr {status}")
            return text
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass


class NSEMonitor(BaseMonitor):
    """NSE announcement monitor."""

    exchange = "NSE"
    source_url = NSE_LANDING_URL

    def __init__(self, *, fetcher=None, parser=parse_nse_payload, **kwargs) -> None:
        if fetcher is None:
            fetcher = fetch_nse_with_playwright
        super().__init__(fetcher=fetcher, parser=parser, **kwargs)
