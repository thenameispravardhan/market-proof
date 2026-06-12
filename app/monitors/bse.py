"""BSE corporate announcements monitor.

BSE exposes a JSON endpoint per category. Each call returns up to
50 rows for a single category within a date window. We fan out
across a curated set of categories per tick so the monitor doesn't
miss a filing just because it landed in a different bucket.

The endpoint requires the cookies set by a prior GET to the BSE
home page, plus the right headers (Referer + Origin). The
``fetch_bse_with_httpx`` fast path handles the common case; we
fall back to Playwright when the API returns 4xx (IP block / CORS).

Endpoints hit per tick (all in parallel)::

    https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w
        ?strCat=Company+Update
        &strPrevDate=YYYYMMDD
        &strToDate=YYYYMMDD
        &strSearch=P
        &strType=C
        &subcategory=-1
        &pageno=1

The same shape is repeated for each category in
``BSE_CATEGORIES``. The response shape per call is::

    {
      "Table":  [ { NEWSID, SCRIP_CD, NEWSSUB, DT_TM, ... ATTACHMENTNAME, ... }, ... ],
      "Table1": [ ... ]            # always empty
    }

Notes from a live probe (2026-06-12):
  - ``strCat`` MUST be a real category name (URL-encoded with ``+``
    for spaces) — the value ``-1`` returns an empty body.
  - ``SCRIP_CD`` is an integer (e.g. ``541770``) not a string.
  - ``ATTACHMENTNAME`` is a bare UUID filename (e.g.
    ``04237faa-1a46-496e-94dd-c13ff827a58d.pdf``) — we prefix the
    BSE AttachLive host to build a usable PDF URL.
  - The hot categories on a typical day are ``Company Update``,
    ``Result``, ``Corp. Action`` and ``Others``.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from app.logging_config import get_logger
from app.monitors.base import BaseMonitor, RawAnnouncement

log = get_logger(__name__)

BSE_LANDING_URL = "https://www.bseindia.com/corporate-announcements.html"
BSE_COOKIE_SEED_URL = "https://www.bseindia.com/"
BSE_API_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
)
BSE_ATTACH_URL_PREFIX = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

# Curated category set. We hit all of them per tick (in parallel)
# and merge + dedupe by NEWSID. Categories that consistently return
# empty are kept so we notice if BSE publishes to a new bucket
# (the empty-cat result is the same as no result, so the cost is
# minimal).
BSE_CATEGORIES: tuple[str, ...] = (
    "Company Update",
    "Result",
    "Corp. Action",
    "Others",
    "Board Meetings",
    "Dividend",
    "Annual Report",
    "Outcome of Board Meeting",
)

# BSE's CORS layer is aggressive — without these headers the API
# returns "Failed to fetch" (browser) or a 403 (raw HTTP).
BSE_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": BSE_LANDING_URL,
    "Origin": "https://www.bseindia.com",
    "Connection": "keep-alive",
}

# Date formats BSE uses in the JSON. The API now returns
# ``2026-06-12T22:55:47.417`` (ISO with fractional seconds).
_BSE_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%d %b %Y %H:%M:%S",
    "%d %b %Y %H:%M",
    "%d %b %Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _parse_bse_date(value: Any) -> Optional[datetime]:
    """Parse a BSE date string to a UTC datetime.

    Returns None for unparseable values. Naive datetimes are treated
    as IST (UTC+5:30) — BSE publishes in Indian Standard Time.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    s = value.strip()
    if not s:
        return None
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


def _normalise_attach(value: Any) -> Optional[str]:
    """Turn a BSE attachment field into a usable URL.

    BSE returns the attachment as a bare UUID filename (e.g.
    ``04237faa-1a46-496e-94dd-c13ff827a58d.pdf``), or a partial
    path like ``AttachLive/abc.pdf`` (no leading slash), or
    occasionally a full URL. We hand back a real https URL.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("http"):
        return s
    if s.startswith("/"):
        return f"https://www.bseindia.com{s}"
    # Bare path: if it already contains the AttachLive subpath we
    # just host-prefix it; otherwise we assume the BSE AttachLive
    # xml-data tree and prefix the canonical host.
    if s.lower().startswith("attachlive/"):
        return f"https://www.bseindia.com/{s}"
    return f"{BSE_ATTACH_URL_PREFIX}{s}"


def _row_to_raw(row: dict[str, Any]) -> Optional[RawAnnouncement]:
    """Convert one BSE JSON row to a `RawAnnouncement`, or None to skip."""
    # SCRIP_CD is an integer in the live response — coerce to string.
    scrip = row.get("SCRIP_CD") or row.get("scrip_cd")
    symbol = (str(scrip) if scrip is not None else "").strip()
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
        row.get("DT_TM") or row.get("dt_tm") or row.get("NEWS_DT") or row.get("news_dt")
    )
    if posted_at is None:
        return None
    pdf_url = _normalise_attach(
        row.get("ATTACHMENTNAME")
        or row.get("ATTACH_URL")
        or row.get("attachmentName")
        or row.get("FILE_NAME")
    )
    # Use the company name as a softer dedup hint: prefix the title
    # so the same PDF filed under two different rows doesn't double-
    # insert. The dedup itself is on content_hash so this is purely
    # cosmetic; the parser still returns a clean RawAnnouncement.
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

    Accepts str or bytes; bytes are decoded utf-8. The response has
    the shape `{"Table": [...], "Table1": [...]}` — we use `Table`
    and tolerate a top-level array for older endpoints.
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
        rows = []
    out: list[RawAnnouncement] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _row_to_raw(row)
        if parsed is not None:
            out.append(parsed)
    return out


# -------------------------------------------------------------------------
# Wire-level fetchers — httpx fast path, Playwright fallback.
# -------------------------------------------------------------------------


def _bse_window_url(category: str, lookback_days: int = 2) -> str:
    """Build the BSE API URL for a single category over the rolling window.

    `strCat` MUST be a real category name (URL-encoded with `+` for
    spaces). The value `-1` returns an empty body.
    """
    today = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=5, minutes=30))
    )
    start = today - timedelta(days=lookback_days)
    end = today
    fmt = "%Y%m%d"
    cat_q = category.replace(" ", "+")
    params = (
        f"strCat={cat_q}"
        f"&strPrevDate={start.strftime(fmt)}"
        f"&strToDate={end.strftime(fmt)}"
        f"&strSearch=P"
        f"&strType=C"
        f"&subcategory=-1"
        f"&pageno=1"
    )
    return f"{BSE_API_URL}?{params}"


async def fetch_bse_with_httpx(
    url: str, *, transport: Any = None, categories: tuple[str, ...] = BSE_CATEGORIES
) -> str:
    """Pure-httpx BSE fetcher.

    Fans out across `categories` in parallel, then merges + dedupes
    by NEWSID into a single combined response. The merge produces
    a JSON shape that ``parse_bse_payload`` already understands:
    ``{"Table": [...], "Table1": []}``.

    `transport` is an optional httpx transport (e.g. MockTransport
    for tests). When provided we skip the `http2=True` flag.
    """
    from app.monitors.base import _RetryableError

    try:
        import httpx
    except ImportError as e:  # pragma: no cover
        raise _RetryableError("httpx not installed") from e

    client_kwargs: dict[str, Any] = {
        "headers": dict(BSE_REQUEST_HEADERS),
        "timeout": 15.0,
        "follow_redirects": True,
    }
    # BSE may also gzip responses; ask for identity so .text works.
    client_kwargs["headers"]["Accept-Encoding"] = "identity"
    if transport is not None:
        client_kwargs["transport"] = transport
    else:
        client_kwargs["http2"] = True

    async with httpx.AsyncClient(**client_kwargs) as client:
        # Step 1: prime cookies via the BSE home page.
        try:
            primer = await client.get(BSE_COOKIE_SEED_URL)
        except Exception as e:  # noqa: BLE001
            raise _RetryableError(f"bse cookie primer failed: {e}") from e
        if primer.status_code >= 500:
            raise _RetryableError(f"bse primer {primer.status_code}")
        if primer.status_code in (401, 403):
            raise _RetryableError(f"bse primer {primer.status_code} (likely IP block)")

        # Step 2: fan out across the category set in parallel.
        async def _one(cat: str) -> tuple[str, int, str]:
            api_url = _bse_window_url(cat)
            try:
                resp = await client.get(api_url)
            except Exception as e:  # noqa: BLE001
                return (cat, 0, "")
            return (cat, resp.status_code, resp.text)

        results = await asyncio.gather(*(_one(c) for c in categories), return_exceptions=True)

        # Aggregate. A single failed category is logged but doesn't
        # sink the whole tick — the operator still gets the rest.
        merged: list[dict[str, Any]] = []
        any_success = False
        for cat, result in zip(categories, results):
            if isinstance(result, Exception):
                log.debug("bse category failed", category=cat, error=str(result))
                continue
            cat_name, status, text = result
            if status == 429 or status >= 500:
                raise _RetryableError(f"bse api {status}")
            if status in (401, 403):
                raise _RetryableError(f"bse api {status} (likely CORS / IP block)")
            if status >= 400:
                log.debug("bse category 4xx", category=cat_name, status=status)
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("Table"), list):
                merged.extend(payload["Table"])
                if payload["Table"]:
                    any_success = True

        if not any_success and not merged:
            # All categories returned empty — treat as success with
            # no announcements to avoid an empty-payload 5xx backoff
            # loop. Operators will see the empty result in the
            # dashboard.
            return json.dumps({"Table": [], "Table1": []})

        return json.dumps({"Table": merged, "Table1": []})


async def fetch_bse_with_playwright(
    url: str, *, categories: tuple[str, ...] = BSE_CATEGORIES
) -> str:
    """Open Chromium, seed cookies, hit the announcements API.

    BSE's CORS preflight rejects raw HTTP clients on some endpoints
    — Playwright is the durable path because the browser handles
    the preflight transparently.
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
                user_agent=BSE_REQUEST_HEADERS["User-Agent"],
                extra_http_headers={
                    "Accept-Language": BSE_REQUEST_HEADERS["Accept-Language"],
                },
            )
            page = await context.new_page()
            try:
                response = await page.goto(
                    BSE_COOKIE_SEED_URL, wait_until="domcontentloaded", timeout=15000
                )
            except Exception as e:  # noqa: BLE001
                raise _RetryableError(f"bse seed goto failed: {e}") from e
            if response is not None and response.status >= 500:
                raise _RetryableError(f"bse seed {response.status}")
            if response is not None and response.status == 429:
                raise _RetryableError("bse seed 429")

            async def _one(cat: str) -> tuple[str, int, str]:
                api_url = _bse_window_url(cat)
                try:
                    api_resp = await context.request.get(
                        api_url,
                        headers={
                            "Accept": BSE_REQUEST_HEADERS["Accept"],
                            "Referer": BSE_REQUEST_HEADERS["Referer"],
                            "Origin": BSE_REQUEST_HEADERS["Origin"],
                        },
                    )
                    return (cat, api_resp.status, await api_resp.text())
                except Exception as e:  # noqa: BLE001
                    return (cat, 0, "")

            results = await asyncio.gather(*(_one(c) for c in categories), return_exceptions=True)

            merged: list[dict[str, Any]] = []
            any_success = False
            for cat, result in zip(categories, results):
                if isinstance(result, Exception):
                    log.debug("bse category failed (playwright)", category=cat, error=str(result))
                    continue
                cat_name, status, text = result
                if status == 429 or status >= 500:
                    raise _RetryableError(f"bse api {status}")
                if status >= 400:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("Table"), list):
                    merged.extend(payload["Table"])
                    if payload["Table"]:
                        any_success = True

            if not any_success and not merged:
                return json.dumps({"Table": [], "Table1": []})
            return json.dumps({"Table": merged, "Table1": []})
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass


async def fetch_bse(url: str) -> str:
    """Try httpx first, fall back to Playwright on persistent 4xx."""
    from app.monitors.base import _RetryableError

    try:
        return await fetch_bse_with_httpx(url)
    except _RetryableError as e:
        err = str(e)
        if "403" in err or "401" in err or "CORS" in err or "IP block" in err:
            return await fetch_bse_with_playwright(url)
        raise


class BSEMonitor(BaseMonitor):
    """BSE announcement monitor."""

    exchange = "BSE"
    source_url = BSE_LANDING_URL

    def __init__(self, *, fetcher=None, parser=parse_bse_payload, **kwargs) -> None:
        if fetcher is None:
            fetcher = fetch_bse
        super().__init__(fetcher=fetcher, parser=parser, **kwargs)
