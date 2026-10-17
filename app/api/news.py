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


@router.get("/items")
def items(since_id: int = 0, q: str = Query("", max_length=100), source: Optional[str] = None,
          hours: Optional[float] = Query(None, gt=0, le=24 * 90), limit: int = Query(500, ge=1, le=2000),
          db: Session = Depends(get_db)):
    """Newest first. since_id gives only rows added after it (the page's 20 s incremental refresh)."""
    stmt, when = _query(db, q, source, hours)
    if since_id:
        stmt = stmt.where(NewsItem.id > since_id)
    rows = db.execute(stmt.order_by(when.desc(), NewsItem.id.desc()).limit(limit)).scalars().all()
    max_id = db.execute(select(func.max(NewsItem.id))).scalar() or 0
    return {"items": [_row(r) for r in rows], "max_id": max_id}


@router.get("/status")
def status(db: Session = Depends(get_db)):
    s = nw.load_settings(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    when = func.coalesce(NewsItem.published_at, NewsItem.seen_at)
    counts = {src: (h1 or 0, h24 or 0, total, last) for src, h1, h24, total, last in db.execute(
        select(NewsItem.source, func.sum(case((when >= now - timedelta(hours=1), 1), else_=0)),
               func.sum(case((when >= now - timedelta(hours=24), 1), else_=0)), func.count(), func.max(NewsItem.seen_at))
        .group_by(NewsItem.source))}
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
                        "last_item": last.replace(tzinfo=timezone.utc).isoformat() if last else None})
    w = nw.news_wire
    return {"settings": s, "sources": sources, "known_sources": nw.SOURCES,
            "collector": {"polls": w.polls, "last_poll_at": w.last_poll_at, "last_poll_ms": w.last_poll_ms}}


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
