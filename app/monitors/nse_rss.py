"""NSE RSS announcement monitor — the fast racer channel.

WHY THIS EXISTS (measured, not assumed): the NSE corporate-announcements
API publishes filings with a median ~30s lag after filed_at. The RSS feed
`Online_announcements.xml` (a static CDN file on nsearchives, no Akamai
challenge, no cookies) was observed carrying a filing ~1 minute old that
the API channel had not yet published — i.e. the RSS can beat the API.
Racing both channels means detection = min(API lag, RSS lag); the
existing content-hash + headline dedupe collapses the loser's copy.

WHAT THE FEED GIVES (and lacks):
  <title>       company NAME ("Vedant Fashions Limited") — NOT the symbol
  <description> the same "... has informed the Exchange about ..." text
                the API uses as headline, plus a "|SUBJECT: ..." suffix
  <link>        the filing document (xbrl .xml or .pdf)
  <pubDate>     "19-Jul-2026 00:02:49" — IST, no timezone marker

The missing symbol is resolved through NSE's own equity master
(EQUITY_L.csv: SYMBOL, NAME OF COMPANY, ...) with EXACT normalized-name
matching only. A name that doesn't match exactly is DROPPED — a fuzzy
match risks trading the wrong stock, and the API channel will deliver
the same filing with the authoritative symbol seconds later anyway.
Fail-safe over fast.
"""
from __future__ import annotations

import csv
import io
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from app.logging_config import get_logger
from app.monitors.base import BaseMonitor, RawAnnouncement

log = get_logger(__name__)

NSE_RSS_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_EQUITY_MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

IST = timezone(timedelta(hours=5, minutes=30))

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Refresh the cached equity master once a day (listings barely change).
_MASTER_TTL_S = 24 * 3600.0


def _master_cache_path() -> Path:
    # Lives beside the Fyers scrip master cache; cwd is the project root
    # (systemd WorkingDirectory), same convention as the SQLite path.
    return Path("data") / "scrip_master" / "nse_equity_l.csv"


def normalize_company_name(name: str) -> str:
    """Uppercase, strip punctuation, collapse whitespace, and drop the
    legal suffix so "Vedant Fashions Limited" == "VEDANT FASHIONS LTD."."""
    s = re.sub(r"[^A-Z0-9 &]", " ", (name or "").upper())
    s = " ".join(s.split())
    s = re.sub(r"\b(LIMITED|LTD)\b\.?$", "", s).strip()
    return s


class NameResolver:
    """company name -> NSE symbol via the official EQUITY_L.csv master.

    Exact normalized-name matches only. `ensure_loaded` is called from
    the (async) fetcher before every parse so the sync parser can do
    pure dict lookups; the master is re-downloaded at most once per day
    and served from the on-disk cache otherwise.
    """

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self._cache_path = cache_path or _master_cache_path()
        self._by_name: dict[str, str] = {}
        self._loaded_at: float = 0.0

    @property
    def size(self) -> int:
        return len(self._by_name)

    def load_csv_text(self, text: str) -> int:
        """Build the lookup from EQUITY_L.csv content. Returns entries."""
        by_name: dict[str, str] = {}
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header or "SYMBOL" not in [h.strip().upper() for h in header]:
            raise ValueError("not an EQUITY_L.csv payload (no SYMBOL header)")
        cols = [h.strip().upper() for h in header]
        i_sym = cols.index("SYMBOL")
        i_name = cols.index("NAME OF COMPANY")
        for row in reader:
            if len(row) <= max(i_sym, i_name):
                continue
            sym = row[i_sym].strip().upper()
            name = normalize_company_name(row[i_name])
            if sym and name:
                by_name[name] = sym
        self._by_name = by_name
        self._loaded_at = time.monotonic()
        return len(by_name)

    async def ensure_loaded(self) -> None:
        """Load from disk cache; refresh from NSE when stale. Any failure
        leaves the previous (possibly empty) map in place — the monitor
        then simply resolves nothing and drops items, which is safe."""
        if self._by_name and (time.monotonic() - self._loaded_at) < _MASTER_TTL_S:
            return
        # 1. Serve from a fresh-enough disk cache.
        try:
            p = self._cache_path
            if p.exists() and (time.time() - p.stat().st_mtime) < _MASTER_TTL_S:
                self.load_csv_text(p.read_text(encoding="utf-8", errors="replace"))
                log.info("nse_rss.master_loaded", source="cache", entries=self.size)
                return
        except Exception:  # noqa: BLE001
            log.debug("nse_rss.master_cache_read_failed")
        # 2. Download and cache.
        try:
            async with httpx.AsyncClient(
                timeout=15.0, headers={"User-Agent": _UA}
            ) as client:
                r = await client.get(NSE_EQUITY_MASTER_URL)
                r.raise_for_status()
                text = r.text
            self.load_csv_text(text)
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                self._cache_path.write_text(text, encoding="utf-8")
            except Exception:  # noqa: BLE001
                log.debug("nse_rss.master_cache_write_failed")
            log.info("nse_rss.master_loaded", source="download", entries=self.size)
        except Exception as e:  # noqa: BLE001
            log.warning("nse_rss.master_download_failed", error=str(e))

    def resolve(self, company_name: str) -> Optional[str]:
        return self._by_name.get(normalize_company_name(company_name))


# Module-level default resolver, shared by the default monitor instance.
_default_resolver = NameResolver()


def parse_pub_date(s: str) -> Optional[datetime]:
    """'19-Jul-2026 00:02:49' (IST, no marker) -> aware UTC."""
    try:
        naive = datetime.strptime(s.strip(), "%d-%b-%Y %H:%M:%S")
    except (ValueError, AttributeError):
        return None
    return naive.replace(tzinfo=IST).astimezone(timezone.utc)


def parse_nse_rss_payload(
    raw: str | bytes,
    url: str,
    *,
    resolver: Optional[NameResolver] = None,
) -> list[RawAnnouncement]:
    """RSS 2.0 -> RawAnnouncement list. Items whose company name cannot
    be EXACTLY resolved to a symbol are dropped (fail-safe; the API
    channel delivers them with the authoritative symbol anyway)."""
    resolver = resolver or _default_resolver
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    root = ET.fromstring(raw)
    out: list[RawAnnouncement] = []
    for item in root.findall(".//item"):
        company_name = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip() or None
        posted_at = parse_pub_date(item.findtext("pubDate") or "")
        if not company_name or posted_at is None:
            continue
        symbol = resolver.resolve(company_name)
        if symbol is None:
            log.debug("nse_rss.unresolved_company", company=company_name)
            continue
        # The description's main text is the same string the API uses as
        # the headline — keeping them identical is what lets the
        # cross-source dedupe collapse the API's copy of this filing.
        headline = desc.split("|SUBJECT:")[0].strip() or company_name
        out.append(
            RawAnnouncement(
                company=symbol,
                title=headline,
                posted_at=posted_at,
                pdf_url=link,
            )
        )
    return out


async def fetch_nse_rss(url: str, *, resolver: Optional[NameResolver] = None) -> str:
    """Fetch the RSS XML. Also keeps the name->symbol master fresh so the
    sync parser can resolve with pure dict lookups. nsearchives is a
    plain CDN — no cookie warmup, no HTTP/2 requirement."""
    await (resolver or _default_resolver).ensure_loaded()
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _UA}) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


class NSERSSMonitor(BaseMonitor):
    """NSE announcement monitor over the RSS racer channel."""

    exchange = "NSE"
    channel = "RSS"
    source_url = NSE_RSS_URL

    def __init__(self, *, fetcher=None, parser=None, resolver=None, **kwargs) -> None:
        res = resolver or _default_resolver
        if fetcher is None:
            async def fetcher(url: str) -> str:  # type: ignore[misc]
                return await fetch_nse_rss(url, resolver=res)
        if parser is None:
            def parser(raw, url):  # type: ignore[misc]
                return parse_nse_rss_payload(raw, url, resolver=res)
        super().__init__(fetcher=fetcher, parser=parser, **kwargs)
