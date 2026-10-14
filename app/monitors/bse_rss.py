"""BSE RSS announcement monitor — BSE's official announcements feed.

WHY THIS EXISTS (measured 2026-10-14): since 2026-09-23 BSE's Akamai edge
answers every non-browser call to the announcements API
(api.bseindia.com/.../AnnSubCategoryGetData) with 403 "Access Denied" —
from the AWS IP and from a home ISP alike, cookies + Referer/Origin or not.
The BSE API monitor has delivered nothing since. BSE still publishes its
own RSS feed, which plain HTTP clients are allowed to read:

    https://www.bseindia.com/data/xml/announcements.xml

WHAT THE FEED GIVES (and lacks):
  <title>       "Exhicon Events Media Solutions Ltd (543895)"
  <scripcode>   543895 — the BSE scrip code, NOT the trading symbol
  <description> the filing's HEADLINE text (the same field the API
                monitor used as the title, so dedupe keys line up)
  <link>        the AttachLive PDF
  <pubDate>     "14-Oct-2026 09:26:44" — IST, no timezone marker
  A rolling window of recent items (35 at a quiet 09:31, 302 during a
  09:34 burst of fund NAV uploads); `Cache-Control: max-age=60`, no ETag.

The scrip code is resolved through Fyers' BSE cash master (BSE_CM.csv,
column 12 = scrip code, column 13 = short symbol), which the instrument
master already keeps in data/scrip_master/. That yields the same short
names the API monitor stored (IKS, VSTTILLERS, ...). A code that doesn't
resolve is DROPPED — fail-safe over fast, same rule as the NSE RSS racer.
"""
from __future__ import annotations

import csv
import io
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from app.logging_config import get_logger
from app.monitors.base import BaseMonitor, RawAnnouncement
from app.monitors.nse_rss import ConditionalRSSFetcher, parse_pub_date

log = get_logger(__name__)

BSE_RSS_URL = "https://www.bseindia.com/data/xml/announcements.xml"
BSE_CM_MASTER_URL = "https://public.fyers.in/sym_details/BSE_CM.csv"

_MASTER_TTL_S = 24 * 3600.0


def _master_path() -> Path:
    from app.services.instrument_master import DEFAULT_DATA_DIR

    return DEFAULT_DATA_DIR / "BSE_CM.csv"


class ScripCodeResolver:
    """BSE scrip code -> short symbol via Fyers' BSE_CM.csv.

    Same duck-typed interface as `nse_rss.NameResolver` (`ensure_loaded`,
    `resolve`, `size`) so `ConditionalRSSFetcher` can drive it unchanged.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._by_code: dict[str, str] = {}
        self._loaded_at: float = 0.0

    @property
    def size(self) -> int:
        return len(self._by_code)

    def load_csv_text(self, text: str) -> int:
        by_code: dict[str, str] = {}
        for row in csv.reader(io.StringIO(text)):
            if len(row) < 14:
                continue
            code, short = row[12].strip(), row[13].strip().upper()
            # Tradable equity only. The ticker suffix is BSE's group:
            # -F = bonds/NCDs, -G = G-secs/T-bills, -INDEX = indices. A
            # company filing is also tagged to its NCD codes (GMR Airports
            # -> 5GMRAL27, 5GMRAL28A, ...); keep only the stock's copy.
            group = row[9].strip().rsplit("-", 1)[-1]
            if code.isdigit() and short and group not in ("F", "G", "INDEX"):
                by_code[code] = short
        self._by_code = by_code
        self._loaded_at = time.monotonic()
        return len(by_code)

    async def ensure_loaded(self) -> None:
        """Load the on-disk master; download it only when it's missing.
        Any failure keeps the previous (possibly empty) map — the monitor
        then resolves nothing and drops items, which is safe."""
        if self._by_code and (time.monotonic() - self._loaded_at) < _MASTER_TTL_S:
            return
        path = self._path or _master_path()
        try:
            if path.exists():
                self.load_csv_text(path.read_text(encoding="utf-8", errors="replace"))
                log.info("bse_rss.master_loaded", source="disk", entries=self.size)
                return
        except Exception:  # noqa: BLE001
            log.debug("bse_rss.master_read_failed")
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                r = await c.get(BSE_CM_MASTER_URL)
                r.raise_for_status()
            self.load_csv_text(r.text)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(r.text, encoding="utf-8")
            except Exception:  # noqa: BLE001
                log.debug("bse_rss.master_write_failed")
            log.info("bse_rss.master_loaded", source="download", entries=self.size)
        except Exception as e:  # noqa: BLE001
            log.warning("bse_rss.master_download_failed", error=str(e))

    def resolve(self, scrip_code: str) -> Optional[str]:
        return self._by_code.get((scrip_code or "").strip())


_default_resolver = ScripCodeResolver()


def parse_bse_rss_payload(
    raw: str | bytes, url: str, *, resolver: Optional[ScripCodeResolver] = None
) -> list[RawAnnouncement]:
    """RSS 2.0 -> RawAnnouncement list; unresolvable scrip codes dropped."""
    resolver = resolver or _default_resolver
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not raw or not raw.strip():
        return []
    # BSE prefixes the XML with a UTF-8 BOM; ElementTree rejects it in a str.
    root = ET.fromstring(raw.lstrip("﻿"))
    out: list[RawAnnouncement] = []
    for item in root.findall(".//item"):
        code = (item.findtext("scripcode") or "").strip()
        posted_at = parse_pub_date(item.findtext("pubDate") or "")
        if not code or posted_at is None:
            continue
        symbol = resolver.resolve(code)
        if symbol is None:
            log.debug("bse_rss.unresolved_scrip", scrip_code=code)
            continue
        company = (item.findtext("title") or "").rsplit("(", 1)[0].strip()
        headline = (item.findtext("description") or "").strip() or company
        if not headline:
            continue
        out.append(
            RawAnnouncement(
                company=symbol,
                title=headline,
                posted_at=posted_at,
                pdf_url=(item.findtext("link") or "").strip() or None,
            )
        )
    return out


class BSERSSMonitor(BaseMonitor):
    """BSE announcement monitor over BSE's official RSS feed."""

    exchange = "BSE"
    channel = "RSS"
    source_url = BSE_RSS_URL

    def __init__(self, *, fetcher=None, parser=None, resolver=None, **kwargs) -> None:
        res = resolver or _default_resolver
        if fetcher is None:
            fetcher = ConditionalRSSFetcher(resolver=res)  # type: ignore[arg-type]
        if parser is None:
            # ponytail: the feed is a rolling window, so a burst bigger than the
            # window between two polls (results-season evenings) can scroll
            # some filings off unseen. We only log it; the upgrade path is a
            # licensed BSE feed, or the API again if BSE reopens it.
            newest: dict[str, Optional[datetime]] = {"t": None}

            def parser(raw, url):  # type: ignore[misc]
                items = parse_bse_rss_payload(raw, url, resolver=res)
                if items:
                    oldest = min(i.posted_at for i in items)
                    if newest["t"] is not None and oldest > newest["t"]:
                        log.warning(
                            "bse_rss.possible_gap",
                            oldest_in_feed=oldest.isoformat(),
                            newest_seen=newest["t"].isoformat(),
                        )
                    newest["t"] = max(newest["t"] or oldest, max(i.posted_at for i in items))
                return items
        super().__init__(fetcher=fetcher, parser=parser, **kwargs)
