"""News page monitor: feed parsing, polite fetching (304 / Retry-After / refusal pause), storage, alerts, API."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import urlsplit

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


def test_nse_titles_become_company_and_subject():
    body = b"""<rss><channel><item><title>Vedant Fashions Limited</title><link>https://nsearchives.nseindia.com/x.xml</link>
<description>VEDANT FASHIONS LIMITED has informed the Exchange about Board Meeting |SUBJECT: Board Meeting Intimation</description>
<pubDate>19-Jul-2026 00:02:49</pubDate></item></channel></rss>"""
    (i,) = nw.parse_feed(body, ist=True, subject=True)
    assert i["title"] == "Vedant Fashions Limited: Board Meeting Intimation"
    assert i["summary"] == "VEDANT FASHIONS LIMITED has informed the Exchange about Board Meeting"
    assert i["published_at"].isoformat() == "2026-07-18T18:32:49"                  # IST without a zone
    assert nw.parse_feed(body)[0]["title"] == "Vedant Fashions Limited"           # other feeds: untouched
    nse = next(f for f in nw.DEFAULT_FEEDS if f["source"] == "NSE")
    assert nse["subject"] and nse["ist"]


def test_every_feed_has_a_group_and_added_sources_ship_off():
    assert {f.get("group") for f in nw.DEFAULT_FEEDS} <= {nw.MARKETS, nw.BUSINESS, nw.OFFICIAL, nw.GLOBAL}
    assert {s for s, on in nw.DEFAULTS["sources"].items() if on} == nw.ORIGINAL   # the defaults poll what they did
    assert not nw.DEFAULTS["extract_sources"]["NSE"]                               # filings, not articles


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
    mode["status"], t = 200, time.time()                                                      # its own interval
    w2 = _wire(handler)
    w2.state[FEED["url"]] = nw.FeedState()
    asyncio.run(w2.fetch(FEED, SETTINGS | {"intervals": {FEED["source"]: 300}}))
    assert 299 < w2.state[FEED["url"]].next_at - t < 302
    assert 290 < w2.next_wait(SETTINGS | {"custom_feeds": [FEED], "sources": {s: False for s in nw.SOURCES}}) <= 300
    fast = {**FEED, "every": 5}                                                       # the feed's own cadence...
    asyncio.run(w2.fetch(fast, SETTINGS))
    assert 4 < w2.state[FEED["url"]].next_at - time.time() <= 5
    asyncio.run(w2.fetch(fast, SETTINGS | {"intervals": {FEED["source"]: 60}}))       # ...unless the operator set one
    assert 59 < w2.state[FEED["url"]].next_at - time.time() <= 60
    n = len(seen)
    asyncio.run(w.poll(SETTINGS | {"custom_feeds": [FEED], "sources": {s: False for s in nw.SOURCES}}))
    assert len(seen) == n                                                                     # paused: no request


def test_a_whole_site_sitemap_keeps_only_the_sections_its_feed_names(isolated_db, db_session):
    url = lambda path, title: (f'<url><loc>https://p.example{path}</loc><news:news><news:title>{title}</news:title>'   # noqa: E731
                               '<news:publication_date>2026-10-18T09:00:00+05:30</news:publication_date></news:news></url>')
    sm = lambda *urls: ('<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'   # noqa: E731
                        + "".join(urls) + "</urlset>").encode()
    f = {"source": "Test", "url": "https://p.example/sitemap.xml", "paths": ("/markets/", "/industry/")}
    w = _wire(lambda req: httpx.Response(200, content=sm(url("/markets/stocks/a", "TCS Q2 profit up"),
                                                         url("/astrology/b", "Horoscope today"))))
    w.state[f["url"]] = nw.FeedState()
    assert asyncio.run(w.fetch(f, SETTINGS)) == 1
    assert [r.title for r in db_session.query(NewsItem)] == ["TCS Q2 profit up"]
    w._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=sm(url("/sports/c", "Match")))))
    w._failed(w.state[f["url"]], 500, "earlier")                             # one miss on record
    assert asyncio.run(w.fetch(f, SETTINGS)) == 0 and w.state[f["url"]].fails == 0   # none in its sections: no failure


def test_a_retitled_story_is_kept_as_an_update_and_nothing_is_pruned_at_zero(isolated_db, db_session):
    item = lambda t: f'<rss><channel><item><title>{t}</title><link>https://pub.example/q2</link></item></channel></rss>'.encode()   # noqa: E731
    w = nw.NewsWire()
    for title in ("HDFC Bank Q2: NII meets estimates", "HDFC Bank Q2: NII meets estimates.",   # re-punctuated: same
                  "HDFC Bank Q2: margins at record low", "HDFC Bank Q2: NII meets estimates",   # back again: known
                  "HDFC Bank Q2: margins at record low"):
        w._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req, b=item(title): httpx.Response(200, content=b)))
        w.state[FEED["url"]] = nw.FeedState()
        asyncio.run(w.fetch(FEED, SETTINGS))
    rows = [(r.url, r.title) for r in db_session.query(NewsItem).order_by(NewsItem.id)]
    assert rows == [("https://pub.example/q2", "HDFC Bank Q2: NII meets estimates"),
                    ("https://pub.example/q2#update-1", "HDFC Bank Q2: margins at record low")]
    db_session.query(NewsItem).update({NewsItem.seen_at: NewsItem.seen_at - timedelta(days=400)})
    db_session.commit()
    w.prune(SETTINGS)                                                         # retention 0 (the default): keep all
    assert db_session.query(NewsItem).count() == 2


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
    assert s["sources"]["SEBI"] is False and s["sources"]["Moneycontrol"] is True   # new sources ship off
    assert client.put("/api/news/settings", json={**body, "intervals": {"BSE": 5}}).status_code == 422
    r = client.put("/api/news/settings", json={**body, "intervals": {"BSE": 60, "Nope": 60}}).json()
    assert r["intervals"] == {"BSE": 60}                                      # unknown source dropped

    now = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=5)).encode()   # inside the 48 h export
    rss = RSS.replace(b"Fri, 16 Oct 2026 09:15:00 +0530", now).replace(b"Fri, 16 Oct 2026 10:00:00 +0530", now)
    w = _wire(lambda req: httpx.Response(200, content=rss))
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
PAYWALLED = ('<html><head><script type="application/ld+json">{"@type":"NewsArticle","description":"Free summary.",'
             '"isAccessibleForFree":false,"hasPart":{"@type":"WebPageElement","isAccessibleForFree":false,'
             '"cssSelector":".paywall"}}</script></head><body><article><h1>Paid story</h1>'
             f'<p>{"Free lead paragraph anyone can read. " * 12}</p><div class="paywall">{PARA}</div></article></body></html>')


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
            elif self.path == "/paid":
                body = PAYWALLED.encode()
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
    for path in ("/old", "/private/x", "/paid"):   # a redirecting link, one robots.txt forbids, a paywalled one
        db_session.add(NewsItem(url=base + path, source="Mint", feed="f", title=f"headline {path}", summary=None))
    db_session.commit()
    s = {**nw.DEFAULTS, "enabled": True, "extract": True}
    assert set(nw.news_wire.pending(s)) == {base + "/old", base + "/private/x", base + "/paid"}
    q = event_bus.subscribe("news")
    try:
        assert asyncio.run(nw.news_wire.extract(s)) == 3
        sent = [q.get_nowait().payload for _ in range(q.qsize())]
    finally:
        event_bus.unsubscribe("news", q)
    assert sent[0] == {"extracted": 1} and sent[-1] == {"extracted": 3}      # pushed as pages land, then the batch
    assert nw.news_wire.pending(s) == []                                     # nothing left: nothing refetched

    art = client.get("/api/news/article", params={"url": base + "/old"}).json()   # followed the 301 to /story
    assert art["status"] == "done" and "zeptoquark" in art["text"] and art["words"] > 50, art
    assert art["meta"]["author"] == "Asha Rao" and art["tables"][0] == [["Quarter", "Revenue"], ["Q2", "1234"]]
    assert art["images"] == [base + "/chart.png"]
    blocked = client.get("/api/news/article", params={"url": base + "/private/x"}).json()
    assert blocked["status"] == "blocked" and blocked["error"] == "robots.txt"
    paid = client.get("/api/news/article", params={"url": base + "/paid"}).json()   # only what it gives away
    assert paid["status"] == "paywalled" and paid["text"].startswith("Free summary.") and "Free lead" in paid["text"]
    assert "Reliance" not in paid["text"], paid

    its = {i["url"]: i for i in client.get("/api/news/items").json()["items"]}
    assert its[base + "/old"]["extract"]["status"] == "done" and its[base + "/old"]["extract"]["chars"] > 300
    found = client.get("/api/news/stocks", params={"id": its[base + "/old"]["id"]}).json()   # the chart button
    assert found["tickers"] == ["NSE:RELIANCE-EQ"]                          # named only in the article text
    deep = client.get("/api/news/items", params={"q": "zeptoquark", "deep": "true"}).json()["items"]
    assert [i["url"] for i in deep] == [base + "/old"]                       # body-only word found via full text
    assert client.get("/api/news/items", params={"q": "zeptoquark"}).json()["items"] == []
    st = client.get("/api/news/status").json()["extract"]
    assert st["with_text"] == 1 and st["by_status"].get("blocked") == 1
    h = next(x for x in st["hosts"] if x["host"] == urlsplit(base).netloc)      # per-site insights
    assert h["done"] == 1 and h["failed"] == 2 and h["words_p50"] > 50 and h["req_24h"] >= 3 and h["refused"] == 0, h   # robots.txt + paywalled
    assert st["requests"]["24h"] >= 3 and st["requests"]["refused_24h"] == 0


def test_pending_is_fair_across_sources_and_skips_paused_hosts(isolated_db, db_session, tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "scrape.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE pauses(host TEXT, start REAL, until REAL, kind TEXT, reason TEXT)")
    con.execute("INSERT INTO pauses VALUES('paused.example', 0, ?, 'restricted', '5 access refusals')", (time.time() + 3600,))
    con.commit()
    con.close()
    monkeypatch.setattr(nw.news_wire, "scrape_db", db)
    for i in range(3):
        db_session.add(NewsItem(url=f"https://paused.example/{i}", source="NDTV", feed="f", title=f"n{i}"))
        db_session.add(NewsItem(url=f"https://small.example/{i}", source="Mint", feed="f", title=f"s{i}"))
    for i in range(30):                                          # a prolific source floods the newest ids
        db_session.add(NewsItem(url=f"https://big.example/{i}", source="Moneycontrol", feed="f", title=f"m{i}"))
    db_session.commit()
    s = {**nw.DEFAULTS, "enabled": True, "extract": True, "extract_sources": {**nw.DEFAULTS["extract_sources"], "NDTV": True}}
    got = nw.news_wire.pending(s, limit=6)
    assert sum("small.example" in u for u in got) == 3                       # was: none (all 25 slots to the big one)
    assert not any("paused.example" in u for u in got)                       # was: paused links filled the batch
    assert len(got) == 6


def test_detection_lag_per_source_skips_first_poll_backlog(client, isolated_db, db_session):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    t0 = now - timedelta(hours=2)
    add = lambda url, src, pub, seen: db_session.add(NewsItem(url=url, source=src, feed="f", title=url, published_at=pub, seen_at=seen))  # noqa: E731
    add("https://a/old", "Mint", t0 - timedelta(days=3), t0)                 # first poll: 3-day-old backlog, ignored
    for i, lag in enumerate((30, 60, 90)):                                   # caught later, 30/60/90 s after publish
        seen = now - timedelta(minutes=30 - i)
        add(f"https://a/{i}", "Mint", seen - timedelta(seconds=lag), seen)
    add("https://a/future", "Mint", now + timedelta(hours=1), now - timedelta(minutes=5))   # future-dated: ignored
    add("https://b/first", "Zee Business", t0, t0)
    for i, lag in enumerate((300, 400, 500)):
        seen = now - timedelta(minutes=20 - i)
        add(f"https://b/{i}", "Zee Business", seen - timedelta(seconds=lag), seen)
    db_session.commit()
    st = client.get("/api/news/status").json()
    mint = next(x["lag"] for x in st["sources"] if x["source"] == "Mint")
    assert mint == {"p50": 60.0, "p90": 90.0, "n": 3}, mint
    assert st["lag"]["n"] == 6 and st["lag"]["fastest"] == "Mint"
