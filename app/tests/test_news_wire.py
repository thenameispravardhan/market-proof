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


# ---------------------------------------------------------------- full text via scraper/scrape.py

PARA = "<p>" + "Reliance Industries said its retail arm grew revenue sharply as demand for groceries rose in every region. " * 5 + "</p>"
ARTICLE = f"""<html><head><title>Reliance retail grows</title><meta name="author" content="Asha Rao">
<meta property="article:published_time" content="2026-10-17T09:15:00+05:30"></head><body><article>
<h1>Reliance retail grows</h1>{PARA * 3}<p>Analysts flagged the zeptoquark margin outlook.</p>
<table><tr><th>Quarter</th><th>Revenue</th></tr><tr><td>Q2</td><td>1234</td></tr></table>
<img src="/chart.png" alt="chart"></article></body></html>"""


def _site():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body, ctype, code = b"", "text/html", 200
            if self.path == "/robots.txt":
                body, ctype = b"User-agent: *\nDisallow: /private\n", "text/plain"
            elif self.path == "/story":
                body = ARTICLE.encode()
            elif self.path == "/old":
                code = 301
            else:
                code = 404
            self.send_response(code)
            if code == 301:
                self.send_header("Location", f"http://{self.headers['Host']}/story")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}"


def test_full_text_extraction_through_the_scraper(client, isolated_db, db_session, tmp_path, monkeypatch):
    base = _site()
    monkeypatch.setattr(nw.news_wire, "scrape_db", tmp_path / "scrape.db")
    monkeypatch.setattr(nw.news_wire, "allow_private", True)
    for path in ("/old", "/private/x"):          # a redirecting link, and one robots.txt forbids
        db_session.add(NewsItem(url=base + path, source="Mint", feed="f", title=f"headline {path}", summary=None))
    db_session.commit()
    s = {**nw.DEFAULTS, "enabled": True, "extract": True}
    assert set(nw.news_wire.pending(s)) == {base + "/old", base + "/private/x"}
    assert asyncio.run(nw.news_wire.extract(s)) == 2
    assert nw.news_wire.pending(s) == []                                     # nothing left: nothing refetched

    art = client.get("/api/news/article", params={"url": base + "/old"}).json()   # followed the 301 to /story
    assert art["status"] == "done" and "zeptoquark" in art["text"] and art["words"] > 50, art
    assert art["meta"]["author"] == "Asha Rao" and art["tables"][0] == [["Quarter", "Revenue"], ["Q2", "1234"]]
    assert art["images"] == [base + "/chart.png"]
    blocked = client.get("/api/news/article", params={"url": base + "/private/x"}).json()
    assert blocked["status"] == "blocked" and blocked["error"] == "robots.txt"

    its = {i["url"]: i for i in client.get("/api/news/items").json()["items"]}
    assert its[base + "/old"]["extract"]["status"] == "done" and its[base + "/old"]["extract"]["chars"] > 300
    deep = client.get("/api/news/items", params={"q": "zeptoquark", "deep": "true"}).json()["items"]
    assert [i["url"] for i in deep] == [base + "/old"]                       # body-only word found via full text
    assert client.get("/api/news/items", params={"q": "zeptoquark"}).json()["items"] == []
    st = client.get("/api/news/status").json()["extract"]
    assert st["with_text"] == 1 and st["by_status"].get("blocked") == 1
