"""News page API: headlines, source health, settings, poll-now, feed test, CSV export. Monitoring only."""
from __future__ import annotations

import asyncio
import csv
import io
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import NewsItem
from app.db.session import get_db
from app.services import news_wire as nw

router = APIRouter(prefix="/api/news", tags=["news"])


def _words(v: list[str]) -> list[str]:
    out = []
    for w in v:
        w = " ".join(str(w).split())[:60]
        if w and w.lower() not in {x.lower() for x in out}:
            out.append(w)
    return out[:50]


class CustomFeed(BaseModel):
    source: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=8, max_length=500)

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        u = urlsplit(v.strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("needs an http(s):// URL")
        return v.strip()


class NewsSettings(BaseModel):
    enabled: bool
    interval_s: int = Field(ge=10, le=600)
    sources: dict[str, bool]
    custom_feeds: list[CustomFeed] = Field(max_length=20)
    watchlist: list[str] = Field(max_length=50)
    muted: list[str] = Field(max_length=50)
    alerts: bool
    retention_days: int = Field(ge=1, le=90)
    extract: bool = False
    extract_sources: dict[str, bool] = {}
    intervals: dict[str, int] = {}

    @field_validator("intervals")
    @classmethod
    def _intervals(cls, v: dict[str, int]) -> dict[str, int]:
        bad = [k for k, n in v.items() if not 10 <= n <= 3600]
        if bad:
            raise ValueError(f"poll interval must be 10-3600 s: {', '.join(bad)}")
        return v

    @field_validator("watchlist", "muted")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return _words(v)


def _row(n: NewsItem) -> dict:
    iso = lambda d: d.replace(tzinfo=timezone.utc).isoformat() if d else None   # noqa: E731 (stored naive UTC)
    return {"id": n.id, "url": n.url, "source": n.source, "title": n.title, "summary": n.summary,
            "published_at": iso(n.published_at), "seen_at": iso(n.seen_at)}


def _query(db: Session, q: str, source: Optional[str], hours: Optional[float]):
    when = func.coalesce(NewsItem.published_at, NewsItem.seen_at)
    stmt = select(NewsItem)
    if q:
        stmt = stmt.where(or_(NewsItem.title.ilike(f"%{q}%"), NewsItem.summary.ilike(f"%{q}%")))
    if source:
        stmt = stmt.where(NewsItem.source == source)
    if hours:
        stmt = stmt.where(when >= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours))
    return stmt, when


def _deep(q: str) -> set[str]:
    """Scraper URLs whose extracted article text mentions q (full-text search)."""
    if not q or not nw.news_wire.scrape_db.exists():
        return set()
    import sqlite3
    con = sqlite3.connect(f"file:{nw.news_wire.scrape_db}?mode=ro", uri=True)
    try:
        return {u for (u,) in con.execute("SELECT url FROM pages WHERE status='done' AND text LIKE ? LIMIT 5000", (f"%{q}%",))}
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


@router.get("/items")
def items(since_id: int = 0, q: str = Query("", max_length=100), source: Optional[str] = None,
          hours: Optional[float] = Query(None, gt=0, le=24 * 90), limit: int = Query(500, ge=1, le=2000),
          deep: bool = False, ids: str = Query("", max_length=3000), db: Session = Depends(get_db)):
    """Newest first. since_id gives only rows added after it (the page's 20 s incremental refresh).
    deep=true also matches headlines whose extracted article text contains q."""
    stmt, when = _query(db, "" if deep else q, source, hours)
    if ids:                                  # re-check specific rows (the page asks again for ones still waiting on text)
        try:
            stmt = stmt.where(NewsItem.id.in_([int(x) for x in ids.split(",") if x][:300]))
        except ValueError:
            raise HTTPException(422, "ids: comma-separated integers")
    if since_id:
        stmt = stmt.where(NewsItem.id > since_id)
    if deep and q:
        hits, ql = _deep(q), q.lower()
        cand = db.execute(stmt.order_by(when.desc(), NewsItem.id.desc()).limit(10000)).scalars().all()
        resolved = nw.lookup([r.url for r in cand], nw.news_wire.scrape_db, text=False) if hits else {}
        rows = [r for r in cand if ql in f"{r.title} {r.summary or ''}".lower()
                or (resolved.get(r.url) or {}).get("url") in hits][:limit]   # the stored row (after redirects)
    else:
        rows = db.execute(stmt.order_by(when.desc(), NewsItem.id.desc()).limit(limit)).scalars().all()
    max_id = db.execute(select(func.max(NewsItem.id))).scalar() or 0
    ext = nw.lookup([r.url for r in rows], nw.news_wire.scrape_db, text=False)
    out = []
    for r in rows:
        e = ext.get(r.url)
        out.append({**_row(r), "extract": {"status": e["status"], "chars": e["chars"] or 0, "error": e["error"],
                                            "fetched": e["fetched"]} if e else None})
    return {"items": out, "max_id": max_id}


@router.get("/article")
def article(url: str = Query(min_length=8, max_length=1000)):
    """What the scraper extracted for one headline: text, metadata (author, tags, image, site), tables, images."""
    import json
    import sqlite3
    e = nw.lookup([url], nw.news_wire.scrape_db).get(url)
    if not e:
        return {"status": "pending"}
    meta = json.loads(e["meta"]) if e.get("meta") else {}
    images = []
    try:
        con = sqlite3.connect(f"file:{nw.news_wire.scrape_db}?mode=ro", uri=True)
        images = [u for (u,) in con.execute("SELECT a.url FROM assets a JOIN asset_pages p ON p.asset = a.url "
                                            "WHERE p.page = ? AND a.kind = 'image' LIMIT 12", (e["url"],))]
        con.close()
    except sqlite3.OperationalError:
        pass
    text = e.get("text") or ""
    return {"status": e["status"], "error": e["error"], "http": e["http"], "title": e.get("title"), "date": e.get("date"),
            "text": text, "words": len(text.split()), "meta": meta,
            "tables": json.loads(e["tables"]) if e.get("tables") else [], "images": images,
            "fetched_at": e["fetched"]}


@router.get("/status")
def status(db: Session = Depends(get_db)):
    s = nw.load_settings(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    when = func.coalesce(NewsItem.published_at, NewsItem.seen_at)
    counts = {src: (h1 or 0, h24 or 0, total, last) for src, h1, h24, total, last in db.execute(
        select(NewsItem.source, func.sum(case((when >= now - timedelta(hours=1), 1), else_=0)),
               func.sum(case((when >= now - timedelta(hours=24), 1), else_=0)), func.count(), func.max(NewsItem.seen_at))
        .group_by(NewsItem.source))}
    lag = _lags(db)
    sources = []
    for f in nw.DEFAULT_FEEDS + [{**c, "custom": True} for c in s["custom_feeds"]]:
        st = nw.news_wire.state.get(f["url"])
        h1, h24, total, last = counts.get(f["source"], (0, 0, 0, None))
        on = s["sources"].get(f["source"], True) if not f.get("custom") else True
        health = ("off" if not on or not s["enabled"] else "waiting" if st is None
                  else "paused" if st.paused_until > time.time() else "error" if st.last_error else "ok")
        sources.append({"source": f["source"], "url": f["url"], "custom": bool(f.get("custom")), "enabled": on,
                        "health": health, "last_ok": st.last_ok if st else None, "error": st.last_error if st else None,
                        "paused_until": st.paused_until if st and st.paused_until > time.time() else None,
                        "items_in_feed": st.items if st else 0, "hour": h1, "day": h24, "total": total,
                        "lag": lag["by_source"].get(f["source"]),
                        "last_item": last.replace(tzinfo=timezone.utc).isoformat() if last else None})
    w = nw.news_wire
    return {"settings": s, "sources": sources, "known_sources": nw.SOURCES, "extract": _extract_stats(db),
            "lag": lag["overall"],
            "collector": {"polls": w.polls, "last_poll_at": w.last_poll_at, "last_poll_ms": w.last_poll_ms}}


def _pct(xs: list[float]) -> dict:
    xs = sorted(xs)
    return {"p50": xs[len(xs) // 2], "p90": xs[int(len(xs) * 0.9)], "n": len(xs)} if xs else {"p50": None, "p90": None, "n": 0}


def _lags(db: Session) -> dict:
    """Detection lag = seen_at - published_at, per source and overall, for headlines seen in the last 24 h.
    A source's first poll is left out: those stories were already in the feed, so their lag only says when
    monitoring started."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    first = dict(db.execute(select(NewsItem.source, func.min(NewsItem.seen_at)).group_by(NewsItem.source)).all())
    per: dict[str, list[float]] = {}
    for src, pub, seen in db.execute(select(NewsItem.source, NewsItem.published_at, NewsItem.seen_at)
                                     .where(NewsItem.seen_at >= now - timedelta(hours=24), NewsItem.published_at.is_not(None))):
        if seen <= first[src] + timedelta(seconds=120):
            continue
        d = (seen - pub).total_seconds()
        if 0 <= d <= 24 * 3600:              # a future date or a re-dated old story says nothing about our speed
            per.setdefault(src, []).append(d)
    by = {k: _pct(v) for k, v in per.items()}
    allv = [x for v in per.values() for x in v]
    fastest = min((k for k, v in by.items() if v["n"] >= 3), key=lambda k: by[k]["p50"], default=None)
    return {"by_source": by, "overall": {**_pct(allv), "fastest": fastest}}


def _host_stats(con) -> tuple[list[dict], dict]:
    """Per article host over the last 24 h: outcomes, words per article, last error, and what we sent it
    (requests, 304s, refusals): the numbers that say whether a site is worth fetching and whether we're too much."""
    now = time.time()
    hosts: dict[str, dict] = {}

    def row(h: str) -> dict:
        return hosts.setdefault(h, {"host": h, "done": 0, "failed": 0, "words": [], "last": None, "error": None,
                                    "req_1h": 0, "req_24h": 0, "not_modified": 0, "refused": 0})
    for url, st, err, fetched, words in con.execute(
            "SELECT url, status, error, fetched, CASE WHEN text IS NULL THEN 0 ELSE length(text) - length(replace(text, ' ', '')) + 1 END"
            " FROM pages WHERE fetched > ? ORDER BY fetched", (now - 86400,)):
        r = row(urlsplit(url).netloc.lower())
        if st == "done":
            r["done"] += 1
            if words:
                r["words"].append(words)
        elif st not in ("queued", "deferred", "redirect", "duplicate"):   # an alias of another row, not an outcome
            r["failed"] += 1
            r["error"] = err or st
        r["last"] = fetched
    for h, t, code in con.execute("SELECT host, t, status FROM requests WHERE t > ?", (now - 86400,)):
        r = row(h)
        r["req_24h"] += 1
        r["req_1h"] += t > now - 3600
        r["not_modified"] += code == "304"
        r["refused"] += code in ("401", "403", "429")
    out = []
    for r in hosts.values():
        w = sorted(r.pop("words"))
        r["words_p50"] = w[len(w) // 2] if w else None
        out.append(r)
    out.sort(key=lambda r: -r["req_24h"])
    return out, {"1h": sum(r["req_1h"] for r in out), "24h": sum(r["req_24h"] for r in out),
                 "refused_24h": sum(r["refused"] for r in out)}


def _extract_stats(db: Session) -> dict:
    """Scraper store summary: outcomes, articles with text, live pauses (hosts it is leaving alone), and the
    extraction lag (headline first seen -> article text stored)."""
    import sqlite3
    out = {"handed": nw.news_wire.extracted, "by_status": {}, "with_text": 0, "paused": [], "lag": _pct([]),
           "hosts": [], "requests": {"1h": 0, "24h": 0, "refused_24h": 0}}
    if not nw.news_wire.scrape_db.exists():
        return out
    con = sqlite3.connect(f"file:{nw.news_wire.scrape_db}?mode=ro", uri=True)
    try:
        out["by_status"] = dict(con.execute("SELECT COALESCE(status, 'queued'), COUNT(*) FROM pages GROUP BY 1"))
        out["with_text"] = con.execute("SELECT COUNT(*) FROM pages WHERE status='done' AND text IS NOT NULL").fetchone()[0]
        out["paused"] = [{"host": h, "until": u, "reason": r} for h, u, r in con.execute(
            "SELECT host, MAX(until), reason FROM pauses GROUP BY host HAVING MAX(until) > ?", (time.time(),))]
        out["hosts"], out["requests"] = _host_stats(con)
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()
    cut = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    recent = db.execute(select(NewsItem.url, NewsItem.seen_at).where(NewsItem.seen_at >= cut)
                        .order_by(NewsItem.id.desc()).limit(2000)).all()
    got = nw.lookup([u for u, _ in recent], nw.news_wire.scrape_db, text=False)
    out["lag"] = _pct([got[u]["fetched"] - seen.replace(tzinfo=timezone.utc).timestamp() for u, seen in recent
                       if u in got and got[u]["status"] == "done" and got[u]["fetched"]
                       and got[u]["fetched"] >= seen.replace(tzinfo=timezone.utc).timestamp()])
    return out


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return nw.load_settings(db)


@router.put("/settings")
async def put_settings(body: NewsSettings, db: Session = Depends(get_db)):
    loop = asyncio.get_running_loop()
    for c in body.custom_feeds:              # never let the server fetch an internal address (it runs on AWS)
        if not await loop.run_in_executor(None, nw.public_host, urlsplit(c.url).hostname):
            raise HTTPException(422, f"{c.url}: not a public host")
    s = body.model_dump()
    s["sources"] = {k: bool(v) for k, v in s["sources"].items() if k in nw.SOURCES}
    s["extract_sources"] = {k: bool(v) for k, v in s["extract_sources"].items() if k in nw.SOURCES}
    known = set(nw.SOURCES) | {c["source"] for c in s["custom_feeds"]}
    s["intervals"] = {k: v for k, v in s["intervals"].items() if k in known}
    nw.save_settings(db, s)
    nw.news_wire.poll_now()
    return nw.load_settings(db)


class FeedTest(BaseModel):
    url: str = Field(min_length=8, max_length=500)


@router.post("/test-feed")
async def test_feed(body: FeedTest):
    """Fetch one URL once and say whether it is a feed: count + three sample titles."""
    u = urlsplit(body.url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise HTTPException(422, "needs an http(s):// URL")
    try:
        r = await nw.news_wire.client().get(body.url.strip())
    except Exception as e:                   # refused host, DNS, timeout...
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    items = nw.parse_feed(r.content[:nw.MAX_BYTES])
    if not items:
        return {"ok": False, "error": "no headlines found: not an RSS / Atom / news-sitemap feed"}
    return {"ok": True, "count": len(items), "sample": [i["title"] for i in items[:3]]}


@router.post("/poll")
def poll():
    nw.news_wire.poll_now()
    return {"ok": True}


@router.get("/export.csv")
def export(q: str = Query("", max_length=100), source: Optional[str] = None,
           hours: float = Query(24, gt=0, le=24 * 90), db: Session = Depends(get_db)):
    stmt, when = _query(db, q, source, hours)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["published_at_utc", "seen_at_utc", "source", "title", "url"])
    for n in db.execute(stmt.order_by(when.desc()).limit(20000)).scalars():
        w.writerow([n.published_at or "", n.seen_at, n.source, n.title, n.url])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="news-{int(time.time())}.csv"'})


@router.delete("/items")
def clear(db: Session = Depends(get_db)):
    n = db.execute(delete(NewsItem)).rowcount
    db.commit()
    return {"deleted": n}
