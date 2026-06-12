"""BSE corporate announcements monitor.

BSE has a friendlier data layer than NSE — corporate announcements are
served by a JSON endpoint that returns a paginated list. The landing
page itself is heavier and JavaScript-rendered, so we go straight to
the data API in production.

Endpoint used by the Playwright fetcher::

    https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w
        ?strCat=-1
        &strPrevDate=20260115
        &strToDate=20260115
        &strSearch=P
        &strType=C
        &subcategory=-1
        &pageno=1

(The page UI hits this from the browser; the actual shape we parse
is the `Table` array of the JSON response.)

The date window is rolling — when the monitor ticks we re-query the
last `lookback_days` (default 2) so we don't miss announcements that
appear after the wire-time clock. The parser is pure and works on
the response text.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from app.logging_config import get_logger
from app.monitors.base import BaseMonitor, RawAnnouncement

log = get_logger(__name__)

BSE_LANDING_URL = "https://www.bseindia.com/corporate-announcements.html"
BSE_API_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
)

# Date formats BSE uses in the JSON. We accept both with and without time.
_BSE_DATE_FORMATS: tuple[str, ...] = (
    "%d %b %Y %H:%M:%S",
    "%d %b %Y %H:%M",
    "%d %b %Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _parse_bse_date(value: Any) -> Optional[datetime]:
    """Parse a BSE date string to a UTC datetime.

    Returns None for unparseable values. Naive datetimes are treated as
    IST (UTC+5:30) — BSE publishes in Indian Standard Time.
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Strip any trailing timezone hint.
    s = re.sub(r"\s*[A-Z]{2,4}$", "", s).strip()
    for fmt in _BSE_DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            ist = timezone(timedelta(hours=5, minutes=30))
            dt = dt.replace(tzinfo=ist)
        return dt.astimezone(timezone.utc)
    return None


def _row_to_raw(row: dict[str, Any]) -> Optional[RawAnnouncement]:
    """Convert one BSE JSON row to a `RawAnnouncement`, or None to skip."""
    # BSE field names — long, capitalised, inconsistent.
    symbol = (row.get("SCRIP_CD") or row.get("scrip_cd") or "").strip()
    if not symbol:
        return None
    title = (
        row.get("HEADLINE")
        or row.get("NEWSSUB")
        or row.get("headline")
        or ""
    ).strip()
    if not title:
        return None
    posted_at = _parse_bse_date(
        row.get("NEWS_DT") or row.get("news_dt") or row.get("DT_TM")
    )
    if posted_at is None:
        return None
    pdf_url = (
        row.get("ATTACHMENTNAME")
        or row.get("ATTACH_URL")
        or row.get("attachmentName")
        or row.get("FILE_NAME")
    )
    if isinstance(pdf_url, str):
        pdf_url = pdf_url.strip() or None
        # BSE sometimes returns bare filenames; prefix the host.
        if pdf_url and not pdf_url.startswith("http"):
            pdf_url = f"https://www.bseindia.com/{pdf_url.lstrip('/')}"
    return RawAnnouncement(
        company=symbol,
        title=title,
        posted_at=posted_at,
        pdf_url=pdf_url,
    )


def parse_bse_payload(
    raw: Union[str, bytes], source_url: str
) -> list[RawAnnouncement]:
    """Pure parser — works on BSE JSON.

    Accepts str or bytes; bytes are decoded utf-8. The response has the
    shape `{"Table": [...], "Table1": [...]}` — we use `Table` and
    tolerate a top-level array for older endpoints.
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
        rows = data.get("Table") or data.get("table") or data.get("data") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    if not isinstance(rows, list):
        return []
    out: list[RawAnnouncement] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _row_to_raw(row)
        if parsed is not None:
            out.append(parsed)
    return out


# -------------------------------------------------------------------------
# Playwright fetcher
# -------------------------------------------------------------------------


def _bse_window_url(lookback_days: int = 2) -> str:
    """Build the BSE API URL for the last `lookback_days` days."""
    today = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=5, minutes=30))
    )
    start = today - timedelta(days=lookback_days)
    end = today
    # BSE wants dd-mm-yyyy? No — its on-the-wire format is yyyymmdd
    # in the `strPrevDate` / `strToDate` parameters.
    fmt = "%Y%m%d"
    params = (
        f"strCat=-1"
        f"&strPrevDate={start.strftime(fmt)}"
        f"&strToDate={end.strftime(fmt)}"
        f"&strSearch=P"
        f"&strType=C"
        f"&subcategory=-1"
        f"&pageno=1"
    )
    return f"{BSE_API_URL}?{params}"


async def fetch_bse_with_playwright(url: str) -> str:
    """Visit BSE landing to get cookies, then hit the announcements API.

    `url` is the BSE landing URL — the fetcher computes the API URL
    from the rolling window.
    """
    from app.monitors.base import _RetryableError

    try:
        from playwright.async_api import async_playwright
    except ImportError as e:  # pragma: no cover
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
            try:
                response = await page.goto(BSE_LANDING_URL, wait_until="domcontentloaded")
            except Exception as e:  # noqa: BLE001
                raise _RetryableError(f"bse landing goto failed: {e}") from e
            if response is not None and response.status >= 500:
                raise _RetryableError(f"bse landing {response.status}")
            if response is not None and response.status == 429:
                raise _RetryableError("bse landing 429")
            api_url = _bse_window_url()
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
                raise _RetryableError(f"bse api evaluate failed: {e}") from e
            if not isinstance(resp, dict):
                raise _RetryableError("bse api unexpected response shape")
            status = int(resp.get("__status", 0))
            text = resp.get("__text", "") or ""
            if status == 429 or status >= 500:
                raise _RetryableError(f"bse api {status}")
            if status >= 400:
                raise _RetryableError(f"bse api {status}")
            return text
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass


class BSEMonitor(BaseMonitor):
    """BSE announcement monitor."""

    exchange = "BSE"
    source_url = BSE_LANDING_URL

    def __init__(self, *, fetcher=None, parser=parse_bse_payload, **kwargs) -> None:
        if fetcher is None:
            fetcher = fetch_bse_with_playwright
        super().__init__(fetcher=fetcher, parser=parser, **kwargs)
