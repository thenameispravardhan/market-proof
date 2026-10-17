"""News page monitor: feed parsing, polite fetching (304 / Retry-After / refusal pause), storage, alerts, API."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.db.models import NewsItem
from app.services import news_wire as nw
from app.services.event_bus import event_bus

RSS = b"""<?xml version="1.0"?><rss><channel><title>C</title><link>https://pub.example/</link>
<image><url>https://pub.example/logo.png</url><title>logo</title></image>
<item><title>ACME Q2 profit up 40%</title><link>https://pub.example/a1</link>
<pubDate>Fri, 16 Oct 2026 09:15:00 +0530</pubDate><description>&lt;p&gt;Shares &amp;amp; bonds&lt;/p&gt;</description></item>
<item><title>Reliance wins order</title><link>https://pub.example/a2</link><pubDate>Fri, 16 Oct 2026 10:00:00 +0530</pubDate></item>
</channel></rss>"""


def test_parse_rss_atom_news_sitemap_and_bse_dates():
    items = nw.parse_feed(RSS)
    assert [i["url"] for i in items] == ["https://pub.example/a1", "https://pub.example/a2"]   # channel image skipped
    assert items[0]["summary"] == "Shares & bonds" and items[0]["published_at"].isoformat() == "2026-10-16T03:45:00"
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title><link href="https://p.example/b"/><updated>2026-10-16T03:45:00Z</updated></entry></feed>'
    assert nw.parse_feed(atom)[0]["url"] == "https://p.example/b"
    sitemap = (b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:n="http://www.google.com/schemas/sitemap-news/0.9" '
               b'xmlns:i="http://www.google.com/schemas/sitemap-image/1.1"><url><loc>https://p.example/story</loc>'
               b'<n:news><n:title>Story</n:title><n:publication_date>2026-10-17T08:45:16+05:30</n:publication_date></n:news>'
               b'<i:image><i:loc>https://img.example/x.jpg</i:loc></i:image></url><url><loc>https://p.example/plain</loc></url></urlset>')
    assert [(i["url"], i["title"]) for i in nw.parse_feed(sitemap)] == [("https://p.example/story", "Story")]   # page, not image
    bse = b"<rss><channel><item><title>ACME (500001)</title><link>https://www.bseindia.com/x.pdf</link><pubDate>17-Oct-2026 09:26:44</pubDate></item></channel></rss>"
    assert nw.parse_feed(bse, ist=True)[0]["published_at"].isoformat() == "2026-10-17T03:56:44"   # IST -> UTC
    assert nw.parse_feed(b"not xml") == [] and nw.parse_feed(b"<urlset><url><loc>https://x/y</loc></url></urlset>") == []


def _wire(handler) -> nw.NewsWire:
    w = nw.NewsWire()
    w._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return w


SETTINGS = {**nw.DEFAULTS, "enabled": True}
FEED = {"source": "Test", "url": "https://pub.example/rss"}


def test_fetch_stores_once_uses_conditional_get_and_respects_refusals(isolated_db, db_session):
    seen, mode = [], {"status": 200}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.headers))
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        if mode["status"] == 429:
            return httpx.Response(429, headers={"Retry-After": "7200"})
        return httpx.Response(200, content=RSS, headers={"ETag": '"v1"'})

    w = _wire(handler)
    w.state[FEED["url"]] = nw.FeedState()
    assert asyncio.run(w.fetch(FEED, SETTINGS)) == 2
    assert asyncio.run(w.fetch(FEED, SETTINGS)) == 0 and seen[-1]["if-none-match"] == '"v1"'   # unchanged -> 304
    assert db_session.query(NewsItem).count() == 2                                            # stored once
    w.state[FEED["url"]].etag = None
    mode["status"] = 429
    asyncio.run(w.fetch(FEED, SETTINGS))
    st = w.state[FEED["url"]]
    assert st.paused_until - time.time() > 7000 and "429" in st.last_error                   # their pause, not ours
    n = len(seen)
    asyncio.run(w.poll(SETTINGS | {"custom_feeds": [FEED], "sources": {s: False for s in nw.SOURCES}}))
    assert len(seen) == n                                                                     # paused: no request


def test_watchlist_alert_only_for_new_items_after_first_read(isolated_db):
    feeds = [RSS]

    def handler(req):
        return httpx.Response(200, content=feeds[0])

    async def go():
        q = event_bus.subscribe("news.alert")
        try:
            w = _wire(handler)
            w.state[FEED["url"]] = nw.FeedState()
            s = SETTINGS | {"alerts": True, "watchlist": ["reliance", "TCS"]}
            await w.fetch(FEED, s)                    # first read = backlog: no alert even though "Reliance" matches
            assert q.empty()
            feeds[0] = RSS.replace(b"</channel>", b"<item><title>TCS beats estimates</title><link>https://pub.example/a3</link></item></channel>")
            await w.fetch(FEED, s)
            evt = await asyncio.wait_for(q.get(), 1)
            assert evt.payload["keywords"] == ["TCS"] and evt.payload["url"] == "https://pub.example/a3"
        finally:
            event_bus.unsubscribe("news.alert", q)

    asyncio.run(go())


def test_guard_refuses_internal_addresses():
    w = nw.NewsWire()

    async def go():
        with pytest.raises(httpx.RequestError):
            await w.client().get("http://169.254.169.254/latest/meta-data/")   # cloud metadata
        await w.client().aclose()

    asyncio.run(go())


def test_api_settings_items_status_export(client, isolated_db, db_session):
    s = client.get("/api/news/settings").json()
    assert s["enabled"] is False and s["interval_s"] == 20                    # ships off, 20 s
    body = {**s, "enabled": True, "watchlist": ["Reliance", " reliance ", "TCS"], "interval_s": 30}
    r = client.put("/api/news/settings", json=body)
    assert r.status_code == 200 and r.json()["watchlist"] == ["Reliance", "TCS"] and r.json()["interval_s"] == 30
    assert client.put("/api/news/settings", json={**body, "interval_s": 5}).status_code == 422
    bad = {**body, "custom_feeds": [{"source": "x", "url": "http://127.0.0.1:8000/feed"}]}
    assert client.put("/api/news/settings", json=bad).status_code == 422      # internal host refused

    w = _wire(lambda req: httpx.Response(200, content=RSS))
    w.state[FEED["url"]] = nw.FeedState()
    asyncio.run(w.fetch(FEED, SETTINGS))
    got = client.get("/api/news/items").json()
    assert [i["title"] for i in got["items"]] == ["Reliance wins order", "ACME Q2 profit up 40%"]   # newest first
    assert client.get(f"/api/news/items?since_id={got['max_id']}").json()["items"] == []
    assert [i["title"] for i in client.get("/api/news/items?q=reliance").json()["items"]] == ["Reliance wins order"]
    st = client.get("/api/news/status").json()
    assert {x["source"] for x in st["sources"]} >= {"Moneycontrol", "Zee Business", "BSE"}
    csv = client.get("/api/news/export.csv?hours=48").text
    assert csv.splitlines()[0].startswith("published_at_utc") and "Reliance wins order" in csv
    assert client.delete("/api/news/items").json()["deleted"] == 2
    client.put("/api/news/settings", json=s)                                  # leave it off
