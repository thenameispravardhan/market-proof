"""News Wire: the News page's headline monitor. Monitoring only, nothing reaches the analyzer or the AI.

Polls public RSS / Atom / news-sitemap feeds (headline, link, time, summary; never article pages) with an honest
user agent and conditional GETs. A source that refuses (403/429) or asks for a pause (Retry-After) is left alone
for that long; a source that errors backs off. Everything the operator can change lives in one AppSetting row,
`news:settings`, edited from the News page; it ships OFF (non-destructive: nothing polls until it's switched on).
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, select

from app.db import session as db_session
from app.db.infra_models import AppSetting
from app.db.models import NewsItem
from app.logging_config import get_logger
from app.services import news_tags
from app.services.event_bus import event_bus

log = get_logger(__name__)

UA = "tradebot-newswire/1.0 (+https://github.com/thenameispravardhan)"
IST = timezone(timedelta(hours=5, minutes=30))
MAX_BYTES = 5_000_000
SETTINGS_KEY = "news:settings"
BURST_QUIET_S = 2 * 3600                         # after a stock's burst alert, none again for it this long

# Settings > Sources lists the sources in these groups.
MARKETS, BUSINESS, OFFICIAL, GLOBAL = "India · markets", "India · business", "Exchanges & regulators", "Global"

# Checked 2026-10-17 with this UA: each returns a live feed and robots.txt allows it. every = how often the feed is
# polled unless the operator set an interval for its source: its cache's lifetime allows it (ET's sitemap is cached
# 4 s and answers 304, Moneycontrol's / Mint's / News18's 30 s, BSE's 60 s at the origin), and the sooner a poll
# lands after a refresh, the sooner a story is seen. min_s = a floor no interval goes under.
# `python scripts/check_news_feeds.py` re-checks them all the way the collector reads them.
DEFAULT_FEEDS: list[dict[str, Any]] = [
    {"source": "Moneycontrol", "group": MARKETS, "every": 10,
     "url": "https://www.moneycontrol.com/news/news-sitemap.xml"},
    {"source": "NDTV Profit", "url": "https://feeds.feedburner.com/ndtvprofit-latest", "group": MARKETS},
    {"source": "NDTV", "url": "https://feeds.feedburner.com/ndtvnews-top-stories", "group": BUSINESS},
    {"source": "Zee Business", "url": "https://www.zeebiz.com/latest.xml", "group": MARKETS},
    {"source": "CNBC-TV18", "url": "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/latest.xml", "group": MARKETS},
    {"source": "CNBC-TV18", "url": "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml", "group": MARKETS},
    # ET and BusinessLine: their Google News sitemaps (listed in robots.txt) show a story ~1.5 min after it is
    # published; their section RSS took 8-14 min (ET) and 3-4 min (measured 2026-10-18). A sitemap is the whole site,
    # so "paths" keeps only these sections. Their other RSS feeds still run: a story they carry first is kept there.
    {"source": "Economic Times", "group": MARKETS, "every": 5,
     "paths": ("/markets/", "/industry/", "/news/economy/", "/news/company/"),
     "url": "https://economictimes.indiatimes.com/etstatic/sitemaps/et/news/sitemap-today.xml"},
    {"source": "Mint", "url": "https://www.livemint.com/rss/markets", "group": MARKETS, "every": 10},
    {"source": "BusinessLine", "group": MARKETS, "every": 10,
     "paths": ("/markets/", "/companies/", "/economy/", "/money-and-banking/", "/portfolio/"),
     "url": "https://www.thehindubusinessline.com/sitemap/googlenews/all/all.xml"},
    {"source": "BSE", "url": "https://www.bseindia.com/data/xml/announcements.xml", "ist": True, "group": OFFICIAL,
     "every": 10},
    # Added 2026-10-17, same check. Off until ticked in Settings > Sources (current behaviour stays the default).
    # Business Standard and Firstpost answer this UA with 403, Financial Express's feed redirects to HTML: left out.
    {"source": "ET Markets", "group": MARKETS,
     "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
    {"source": "Times of India", "url": "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms", "group": BUSINESS},
    {"source": "Business Today", "url": "https://www.businesstoday.in/rssfeeds/?id=225346", "group": MARKETS},
    {"source": "The Hindu", "url": "https://www.thehindu.com/business/markets/feeder/default.rss", "group": MARKETS},
    {"source": "News18", "url": "https://www.news18.com/commonfeeds/v1/eng/rss/business.xml", "group": BUSINESS,
     "every": 10},
    {"source": "RBI", "url": "https://www.rbi.org.in/pressreleases_rss.xml", "group": OFFICIAL},
    {"source": "SEBI", "url": "https://www.sebi.gov.in/sebirss.xml", "group": OFFICIAL},   # dates only: no published_at
    {"source": "Mint Companies", "url": "https://www.livemint.com/rss/companies", "group": BUSINESS, "every": 10},
    {"source": "ET Industry", "group": BUSINESS,
     "url": "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms"},
    {"source": "BusinessLine Companies", "group": BUSINESS,
     "url": "https://www.thehindubusinessline.com/companies/feeder/default.rss"},
    {"source": "Indian Express", "url": "https://indianexpress.com/section/business/feed/", "group": BUSINESS},
    {"source": "ThePrint", "url": "https://theprint.in/category/economy/feed/", "group": BUSINESS},
    # Also tried: Google News (robots.txt denies bots), PIB + Zee Business markets (403), Financial Express, Deccan
    # Herald, Moneylife, Outlook Business, NDTV Profit markets (feed gone), Investing.com (US filings, not India).
    # Added 2026-10-17 once scripts/check_news_feeds.py passed them from a GitHub runner (CI job "News feeds"). The
    # same run: Business Standard and Financial Express 403, Yahoo Finance and Entrackr 404, WSJ Markets, India
    # Today and Moneycontrol's RSS (results, IPO, market reports) frozen for over a year: left out. Indian Express
    # (above) answered that runner 403 too, kept as it was.
    {"source": "ET Earnings", "group": MARKETS,
     "url": "https://economictimes.indiatimes.com/markets/stocks/earnings/rssfeeds/2143429.cms"},
    {"source": "ET IPO", "group": MARKETS,
     "url": "https://economictimes.indiatimes.com/markets/ipos/fpos/rssfeeds/14655708.cms"},
    {"source": "ET Commodities", "group": MARKETS,
     "url": "https://economictimes.indiatimes.com/markets/commodities/rssfeeds/1808152121.cms"},
    {"source": "CNBC-TV18 Economy", "group": BUSINESS,
     "url": "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/economy.xml"},
    {"source": "ET Economy", "group": BUSINESS,
     "url": "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms"},
    {"source": "Mint Economy", "url": "https://www.livemint.com/rss/economy", "group": BUSINESS, "every": 10},
    {"source": "Mint Money", "url": "https://www.livemint.com/rss/money", "group": BUSINESS, "every": 10},
    {"source": "BusinessLine Economy", "group": BUSINESS,
     "url": "https://www.thehindubusinessline.com/economy/feeder/default.rss"},
    {"source": "The Hindu Economy", "group": BUSINESS,
     "url": "https://www.thehindu.com/business/Economy/feeder/default.rss"},
    {"source": "Hindustan Times", "group": BUSINESS,
     "url": "https://www.hindustantimes.com/feeds/rss/business/rssfeed.xml"},
    {"source": "Inc42", "url": "https://inc42.com/feed/", "group": BUSINESS},
    # NSE's announcements file: a static CDN copy (no cookies), titles are company names, so the headline is
    # "Company: <its |SUBJECT: suffix>"; times are IST without a zone. It timed out from the (US) GitHub runner:
    # NSE throttles foreign IPs, and the analyzer's NSE RSS racer was measured on this file from India.
    {"source": "NSE", "url": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml", "ist": True,
     "subject": True, "group": OFFICIAL, "every": 10},
    {"source": "RBI Notifications", "url": "https://www.rbi.org.in/notifications_rss.xml", "group": OFFICIAL},
    {"source": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "group": GLOBAL,
     "min_s": 120},   # a few releases a day; 20 s polls only collected its CDN's stray 404s
    {"source": "CNBC", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "group": GLOBAL},
    {"source": "CNBC Asia", "url": "https://www.cnbc.com/id/19832390/device/rss/rss.html", "group": GLOBAL},
    {"source": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "group": GLOBAL},
    {"source": "BBC Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml", "group": GLOBAL},
    {"source": "Nikkei Asia", "url": "https://asia.nikkei.com/rss/feed/nar", "group": GLOBAL},
    {"source": "OilPrice", "url": "https://oilprice.com/rss/main", "group": GLOBAL},
]
SOURCES = sorted({f["source"] for f in DEFAULT_FEEDS})
SOURCE_GROUPS = {f["source"]: f["group"] for f in DEFAULT_FEEDS}
ORIGINAL = {"Moneycontrol", "NDTV Profit", "NDTV", "Zee Business", "CNBC-TV18", "Economic Times", "Mint",
            "BusinessLine", "BSE"}
NEW_OFF = set(SOURCES) - ORIGINAL                # every source added since ships off: the defaults poll what they did
# Full text comes from scraper/scrape.py (the operator's crawler), into its own SQLite store. These sources'
# article links refuse an honest client (BSE AttachLive PDFs 503/406, ndtvprofit.com Akamai 403, ndtv.com 403 x5 on
# 2026-10-17, which the scraper then paused), are exchange filings (NSE: XBRL / PDF) or sit behind a paywall: off.
SCRAPE_DB = Path("data/scrape.db")
EXTRACT_OFF = {"BSE", "NDTV Profit", "NDTV", "NSE", "MarketWatch", "Nikkei Asia"}

DEFAULTS: dict[str, Any] = {
    "enabled": False,        # off until the operator turns it on from the News page
    "interval_s": 20,
    "sources": {s: s not in NEW_OFF for s in SOURCES},
    "intervals": {},         # {source: seconds}: its own poll interval; a source not listed uses interval_s
    "custom_feeds": [],      # [{"source": "My blog", "url": "https://..."}]
    "watchlist": [],         # highlighted on the page; optional alerts
    "muted": [],             # hidden on the page
    "alerts": False,         # send watchlist hits to notification channels subscribed to "news"
    "retention_days": 0,     # days a headline is kept; 0 = every headline, for good
    "extract": False,        # fetch each new headline's article with the scraper: text, metadata, tables, PDF text
    "extract_sources": {s: s not in EXTRACT_OFF for s in SOURCES},
    "bursts": False,         # alert when burst_sources+ outlets name one stock within burst_minutes (breaking news)
    "burst_sources": 3,
    "burst_minutes": 30,
}


# ---------------------------------------------------------------- settings

def load_settings(db=None) -> dict[str, Any]:
    own = db is None
    db = db or db_session.SessionLocal()
    try:
        row = db.get(AppSetting, SETTINGS_KEY)
        saved = json.loads(row.value) if row and row.value else {}
    except (ValueError, TypeError):
        saved = {}
    finally:
        if own:
            db.close()
    out = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    out["sources"] = {**DEFAULTS["sources"], **(saved.get("sources") or {})}
    out["extract_sources"] = {**DEFAULTS["extract_sources"], **(saved.get("extract_sources") or {})}
    return out


def save_settings(db, s: dict[str, Any]) -> None:
    row = db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        db.add(AppSetting(key=SETTINGS_KEY, value=json.dumps(s)))
    else:
        row.value = json.dumps(s)
    db.commit()


def feeds_for(s: dict[str, Any]) -> list[dict[str, Any]]:
    fs = [f for f in DEFAULT_FEEDS if s["sources"].get(f["source"], True)]
    return fs + [{"source": c["source"], "url": c["url"], "custom": True} for c in s["custom_feeds"]]


# ---------------------------------------------------------------- safety

def public_host(host: str) -> bool:
    """False for loopback / private / link-local (cloud metadata) / reserved: the server is on AWS, and a
    custom feed URL must never make it fetch an internal address."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for *_, sa in infos:
        ip = ipaddress.ip_address(sa[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


async def _guard(request: httpx.Request) -> None:            # every request and every redirect hop
    if not await asyncio.get_running_loop().run_in_executor(None, public_host, request.url.host):
        raise httpx.RequestError(f"refused: {request.url.host} is not a public host", request=request)


# ---------------------------------------------------------------- parsing

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _when(v: Optional[str], ist: bool) -> Optional[datetime]:
    """RSS RFC-822, ISO 8601, or BSE's '17-Oct-2026 09:26:44' (IST, no zone) -> naive UTC."""
    if not v:
        return None
    v = v.strip()
    d = None
    try:
        d = parsedate_to_datetime(v)
    except (TypeError, ValueError):
        for parse in (lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),
                      lambda x: datetime.strptime(x, "%d-%b-%Y %H:%M:%S")):
            try:
                d = parse(v)
                break
            except ValueError:
                continue
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=IST if ist else timezone.utc)
    return d.astimezone(timezone.utc).replace(tzinfo=None)


def _text(e: Optional[ET.Element]) -> str:
    return " ".join("".join(e.itertext()).split()) if e is not None else ""


def parse_feed(body: bytes, ist: bool = False, subject: bool = False) -> list[dict[str, Any]]:
    """Items of an RSS / Atom / news-sitemap document. A plain sitemap (no titles) gives []. Stdlib ElementTree
    resolves no external entities, and Python's expat caps entity expansion. subject: the feed's titles are company
    names and its descriptions end in "|SUBJECT: ..." (NSE): the headline becomes "Company: subject"."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter():
        if _local(it.tag) not in ("item", "entry", "url"):
            continue
        kid: dict[str, ET.Element] = {}
        for c in it.iter():
            kid.setdefault(_local(c.tag), c)            # first wins: <image:loc> comes after the page's <loc>
        first = lambda *names: next((kid[n] for n in names if n in kid), None)   # noqa: E731 (an Element with no
        link = kid.get("link")                                                     # children is falsy: never `or` them)
        url = ((link.get("href") or (link.text or "")).strip() if link is not None else _text(kid.get("loc")))
        title = _text(kid.get("title"))
        if not url.startswith(("http://", "https://")) or not title:
            continue
        summary = _text(first("description", "summary"))
        summary = html.unescape(re.sub(r"<[^>]+>", " ", summary))
        if subject and "|SUBJECT:" in summary:
            summary, subj = summary.split("|SUBJECT:", 1)
            title = f"{title}: {' '.join(subj.split())}" if subj.strip() else title
        out.append({"url": url[:1000], "title": title[:1000], "summary": " ".join(summary.split())[:600] or None,
                    "published_at": _when(_text(first("pubDate", "published", "publication_date", "updated", "date")), ist)})
    return out


# ---------------------------------------------------------------- collector

@dataclass
class FeedState:
    etag: Optional[str] = None
    modified: Optional[str] = None
    next_at: float = 0.0
    paused_until: float = 0.0
    fails: int = 0
    primed: bool = False                 # first good read done: its items are backlog, never alerted
    last_ok: Optional[float] = None
    last_error: Optional[str] = None
    last_status: Optional[int] = None
    items: int = 0
    last_new: int = 0


@dataclass
class NewsWire:
    state: dict[str, FeedState] = field(default_factory=dict)
    polls: int = 0
    last_poll_at: Optional[float] = None
    last_poll_ms: Optional[int] = None
    _wake: Optional[asyncio.Event] = None
    _client: Optional[httpx.AsyncClient] = None
    _pruned_at: float = 0.0
    scrape_db: Path = SCRAPE_DB
    allow_private: bool = False          # tests only: lets the scraper reach a local mock server
    extracted: int = 0                   # articles handed to the scraper since start
    _hosts: dict = field(default_factory=dict)   # the scraper's per-host state (robots.txt, gaps, pauses) across runs
    _new: list = field(default_factory=list)     # this poll's headlines from primed feeds, for the burst check
    _burst_at: dict = field(default_factory=dict)   # symbol -> when its last burst alert went out

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15.0, follow_redirects=True, headers={"User-Agent": UA},
                event_hooks={"request": [_guard]}, limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120))
        return self._client

    def poll_now(self) -> None:
        for st in self.state.values():
            if st.paused_until <= time.time():
                st.next_at = 0.0
        if self._wake:
            self._wake.set()

    async def run(self) -> None:
        self._wake = asyncio.Event()
        while True:
            try:
                s = load_settings()
                if s["enabled"]:
                    await self.poll(s)
                    self.prune(s)
                wait = self.next_wait(s) if s["enabled"] else 5
            except asyncio.CancelledError:
                raise
            except Exception:                    # one bad poll never stops the monitor
                log.exception("news_wire.poll_failed")
                wait = 30
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    def next_wait(self, s: dict[str, Any]) -> float:
        """Seconds until the soonest feed is due (each has its own interval), at least 1."""
        due = [max(st.next_at, st.paused_until) for f in feeds_for(s) if (st := self.state.get(f["url"]))]
        return max(1.0, min(due) - time.time()) if due else s["interval_s"]

    async def poll(self, s: dict[str, Any]) -> int:
        t0, now = time.time(), time.time()
        due = [f for f in feeds_for(s)
               if self.state.setdefault(f["url"], FeedState()).next_at <= now and self.state[f["url"]].paused_until <= now]
        results = await asyncio.gather(*(self.fetch(f, s) for f in due))
        new = sum(results)
        fresh, self._new = self._new, []
        if fresh and s["bursts"]:
            await self._bursts(fresh, s)
        self.polls += 1
        self.last_poll_at, self.last_poll_ms = time.time(), int((time.time() - t0) * 1000)
        await event_bus.publish("news", {"new": new, "polls": self.polls, "last_poll_at": self.last_poll_at,
                                         "last_poll_ms": self.last_poll_ms})   # the News page updates without polling
        return new

    async def fetch(self, f: dict[str, Any], s: dict[str, Any]) -> int:
        st, url = self.state[f["url"]], f["url"]
        st.next_at = time.time() + max(s["intervals"].get(f["source"], f.get("every", s["interval_s"])), f.get("min_s", 0))
        hdrs = {k: v for k, v in (("If-None-Match", st.etag), ("If-Modified-Since", st.modified)) if v}
        try:
            async with self.client().stream("GET", url, headers=hdrs) as r:
                body = bytearray()
                if r.status_code == 200:
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise ValueError(f"feed over {MAX_BYTES} bytes")
        except (httpx.HTTPError, ValueError) as e:
            return self._failed(st, None, f"{type(e).__name__}: {str(e)[:150]}")
        st.last_status = r.status_code
        if r.status_code == 304:
            st.last_ok, st.last_error, st.fails, st.last_new = time.time(), None, 0, 0
            return 0
        if r.status_code in (403, 429) or (r.status_code == 503 and r.headers.get("retry-after")):
            ra = _retry_after(r.headers.get("retry-after"))
            st.fails += 1
            pause = max(ra or 0, min(900 * 2 ** (st.fails - 1), 6 * 3600))   # their pause, or 15 min doubling to 6 h
            st.paused_until = time.time() + pause
            st.last_error = f"HTTP {r.status_code}: paused {pause / 60:.0f} min"
            return 0
        if r.status_code != 200:
            return self._failed(st, r.status_code, f"HTTP {r.status_code}")
        st.etag, st.modified = r.headers.get("etag"), r.headers.get("last-modified")
        # Moneycontrol's 500 KB sitemap takes ~25 ms to parse, every 10 s: off the loop the trading path shares
        items = await asyncio.get_running_loop().run_in_executor(
            None, lambda: parse_feed(bytes(body), ist=f.get("ist", False), subject=f.get("subject", False)))
        if not items:
            return self._failed(st, 200, "no items (not a feed?)")
        if f.get("paths"):                       # a whole-site sitemap: only the sections the feed names
            items = [i for i in items if urlsplit(i["url"]).path.startswith(f["paths"])]
        fresh = self._store(f["source"], url, items)
        st.last_ok, st.last_error, st.fails, st.items, st.last_new = time.time(), None, 0, len(items), len(fresh)
        if fresh:                                # on screen now, not when the round's slowest feed answers
            await event_bus.publish("news", {"new": len(fresh)})
        if st.primed and fresh and s["alerts"] and s["watchlist"]:
            await self._alert(fresh, s["watchlist"])
        if st.primed:
            self._new.extend(fresh)
        st.primed = True
        return len(fresh)

    def _failed(self, st: FeedState, status: Optional[int], err: str) -> int:
        st.fails += 1
        st.last_status = status
        if st.fails >= 2:        # one miss is noise (the Fed's CDN 404s about 1 poll in 16 between two 304s)
            st.last_error = err
        st.next_at = time.time() + min(30 * 2 ** st.fails, 600)    # back off: 1, 2, 4 ... up to 10 min
        return 0

    def _store(self, source: str, feed: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        db = db_session.SessionLocal()
        try:
            urls = [i["url"] for i in items]
            known = dict(db.execute(select(NewsItem.url, NewsItem.title).where(NewsItem.url.in_(urls))).all())
            fresh, seen = [], set()
            for i in items:
                url = i["url"]
                if url in seen:
                    continue
                seen.add(url)
                if url in known:
                    if _same(known[url], i["title"]):
                        continue
                    # The feed re-titled a story it already ran ("HDFC Bank Q2: NII meets estimates" -> "...: margins
                    # at record low"): new information, kept as a row of its own beside every earlier one.
                    prior = db.execute(select(NewsItem.title).where(NewsItem.url >= f"{url}#update-",
                                                                    NewsItem.url < f"{url}#update.")).scalars().all()
                    if any(_same(t, i["title"]) for t in prior):
                        continue
                    url = f"{url}#update-{len(prior) + 1}"
                fresh.append({**i, "url": url, "source": source})
                db.add(NewsItem(url=url, source=source, feed=feed, title=i["title"], summary=i["summary"],
                                published_at=i["published_at"]))
            db.commit()
            return fresh
        finally:
            db.close()

    async def _alert(self, fresh: list[dict[str, Any]], watchlist: list[str]) -> None:
        # A watchlist word that names a stock also hits every headline tagged with it ("Reliance" -> "RIL shares
        # jump"). Tagging is CPU work, so it runs off the event loop the trading path shares.
        named, tagged = await asyncio.get_running_loop().run_in_executor(None, _alert_tags, fresh, watchlist)
        for i, stocks in zip(fresh, tagged):
            hay = f"{i['title']} {i.get('summary') or ''}".lower()
            hits = [w for w in watchlist if w.lower() in hay or stocks & set(named.get(w, ()))]
            if hits:
                await event_bus.publish("news.alert", {"keywords": hits, "title": i["title"], "source": i["source"],
                                                       "url": i["url"]})

    async def _bursts(self, fresh: list[dict[str, Any]], s: dict[str, Any]) -> None:
        """Breaking news: a stock this poll's headlines name that burst_sources+ outlets have named within
        burst_minutes. One alert per stock per burst (none again for 2 h); off the event loop like _alert."""
        found = await asyncio.get_running_loop().run_in_executor(None, _burst_scan, fresh, s)
        for b in found:
            if time.time() - self._burst_at.get(b["symbol"], 0) < BURST_QUIET_S:
                continue
            self._burst_at[b["symbol"]] = time.time()
            await event_bus.publish("news.alert", {
                "keywords": [f"{b['name']}: {len(b['sources'])} outlets in {s['burst_minutes']} min"],
                "title": b["title"], "source": ", ".join(sorted(b["sources"])), "url": b["url"],
                "symbol": b["symbol"], "burst": True})

    def prune(self, s: dict[str, Any]) -> None:
        if not s["retention_days"] or time.time() - self._pruned_at < 3600:   # 0: keep every headline
            return
        self._pruned_at = time.time()
        db = db_session.SessionLocal()
        try:
            cut = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=s["retention_days"])
            db.execute(delete(NewsItem).where(NewsItem.seen_at < cut))
            db.commit()
        finally:
            db.close()


    # ------------------------------------------------------------ full text (scraper/scrape.py)

    async def run_extract(self) -> None:
        while True:
            try:
                s = load_settings()
                n = await self.extract(s) if s["enabled"] and s["extract"] else 0
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("news_wire.extract_failed")
                n = 0
            await asyncio.sleep(5 if n else 30)

    def pending(self, s: dict[str, Any], limit: int = 25) -> list[str]:
        """Headlines (12 h) from sources with full text on that the scraper hasn't finished, newest first and shared
        round-robin across sources, so a prolific source (Moneycontrol's sitemap) can't starve the rest. Links on a
        host the scraper has paused are skipped rather than filling the batch; they stay pending until it ends."""
        custom = {c["source"] for c in s["custom_feeds"]}
        on = [k for k, v in s["extract_sources"].items() if v and s["sources"].get(k, True)] + sorted(custom)
        db = db_session.SessionLocal()
        try:
            cut = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=12)
            rows = db.execute(select(NewsItem.source, NewsItem.url).where(NewsItem.source.in_(on), NewsItem.seen_at >= cut)
                              .order_by(NewsItem.id.desc()).limit(3000)).all()
        finally:
            db.close()
        state = lookup([u for _, u in rows], self.scrape_db, text=False)
        paused = paused_hosts(self.scrape_db)
        queues: dict[str, list[str]] = defaultdict(list)
        for src, u in rows:
            if (state.get(u) or {}).get("status") in (None, "queued", "deferred") and urlsplit(u).netloc.lower() not in paused:
                queues[src].append(u)
        out: list[str] = []
        while len(out) < limit and any(queues.values()):
            for q in queues.values():
                if q and len(out) < limit:
                    out.append(q.pop(0))
        return out

    async def extract(self, s: dict[str, Any]) -> int:
        urls = self.pending(s)
        if not urls:
            return 0
        from scraper import scrape          # heavy (trafilatura): imported only once full text is switched on

        argv = ["--db", str(self.scrape_db), "--workers", "1", "--concurrency", "4", "--max-delay", "30",
                "--max-pages", str(len(urls) * 3)] + (["--allow-private"] if self.allow_private else [])
        a = scrape.parse_args(argv)
        # one polite profile for every article host: html only (no link-walking, no browser), 3 s apart,
        # one at a time per host, one retry; a refusing host is paused (persisted in scrape.db)
        a.sources = {urlsplit(scrape.normalize(u) or u).netloc: {"method": "html", "delay": 3, "per_host": 1,
                                                                   "retries": 1, "pause": 3600} for u in urls}
        c = scrape.Crawler(a)
        c.hosts = self._hosts
        loop, pushed = asyncio.get_running_loop(), [0.0]

        def saved(url: str, status: Optional[str]) -> None:   # each stored page, at most every 2 s: the page's
            if time.time() - pushed[0] >= 2:                   # scraper counts and TEXT badges move as it works,
                pushed[0] = time.time()                        # not once per batch of 25
                loop.create_task(event_bus.publish("news", {"extracted": 1}))
        c.on_save = saved
        try:
            await c.run(urls)
        finally:
            c.db.close()
            self._hosts = c.hosts
        self.extracted += len(urls)
        await event_bus.publish("news", {"extracted": len(urls)})
        return len(urls)


def _alert_tags(fresh: list[dict[str, Any]], watchlist: list[str]) -> tuple[dict[str, list[str]], list[set[str]]]:
    return news_tags.watch_stocks(watchlist), [set(news_tags.stocks(i["title"], i.get("summary"))) for i in fresh]


def _burst_scan(fresh: list[dict[str, Any]], s: dict[str, Any]) -> list[dict[str, Any]]:
    """The stocks in `fresh` that burst_sources+ outlets named in headlines published (or, undated, first seen)
    within the last burst_minutes, muted headlines left out. Each with its outlets and its latest headline."""
    names = {t for i in fresh for t in news_tags.stocks(i["title"], i.get("summary"))}
    if not names:
        return []
    cut = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=s["burst_minutes"])
    muted = [w.lower() for w in s["muted"]]
    db = db_session.SessionLocal()
    try:
        rows = db.execute(select(NewsItem.source, NewsItem.title, NewsItem.summary, NewsItem.url, NewsItem.published_at,
                                 NewsItem.seen_at).where(NewsItem.seen_at >= cut)).all()
    finally:
        db.close()
    by: dict[str, dict[str, Any]] = {}
    for src, title, summary, url, pub, seen in rows:
        when = pub if pub and pub <= seen else seen
        if when < cut or any(m in title.lower() for m in muted):
            continue
        for sym in news_tags.stocks(title, summary):
            if sym in names:
                b = by.setdefault(sym, {"symbol": sym, "name": sym.split(":")[-1].rsplit("-", 1)[0],
                                        "sources": set(), "when": when, "title": title, "url": url})
                b["sources"].add(src)
                if when >= b["when"]:
                    b.update(when=when, title=title, url=url)
    return [b for b in by.values() if len(b["sources"]) >= s["burst_sources"]]


def paused_hosts(db_path: Path) -> set[str]:
    """Hosts the scraper is leaving alone right now (its persisted pauses)."""
    if not db_path.exists():
        return set()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {h for (h,) in con.execute("SELECT host FROM pauses GROUP BY host HAVING MAX(until) > ?", (time.time(),))}
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


def lookup(urls: list[str], db_path: Path, text: bool = True) -> dict[str, dict[str, Any]]:
    """Scraper rows for these headline URLs, keyed by the headline URL. Follows the scraper's URL normalisation
    and one redirect hop (an article URL that 301s to its canonical address). {} until scrape.db exists."""
    if not urls or not db_path.exists():
        return {}
    from scraper.scrape import normalize

    cols = "url, status, http, final_url, error, fetched, length(text)" + (", title, date, text, meta, tables" if text else "")
    norm = {u: normalize(u) or u for u in urls}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def rows(keys):
            out = {}
            keys = list(keys)
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                for r in con.execute(f"SELECT {cols} FROM pages WHERE url IN ({','.join('?' * len(part))})", part):
                    out[r[0]] = r
            return out

        got = rows(set(norm.values()))
        hops = {r[3] for r in got.values() if r[1] == "redirect" and r[3]}
        got.update(rows(hops - set(got)))
        names = [c.strip().split(" ")[0].replace("length(text)", "chars") for c in cols.split(",")]
        out = {}
        for u, n in norm.items():
            r = got.get(n)
            if r and r[1] == "redirect" and r[3] in got:
                r = got[r[3]]
            if r:
                out[u] = dict(zip(names, r))
        return out
    except sqlite3.OperationalError:                 # scrape.db exists but has no pages table yet
        return {}
    finally:
        con.close()


def _same(a: str, b: str) -> bool:
    """One headline re-punctuated ("details here." / "details here"): no update."""
    return re.sub(r"\W+", " ", a.lower()).strip() == re.sub(r"\W+", " ", b.lower()).strip()


def _retry_after(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    if v.strip().isdigit():
        return float(v)
    try:
        d = parsedate_to_datetime(v)
        return max(0.0, (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


news_wire = NewsWire()
