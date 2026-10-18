"""python -m pytest test_news.py -q : the X API reader (fake API) and the dashboard's JSON endpoints."""
import asyncio
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SEEN = []                                    # query strings the fake X API received
AGO = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 120))   # inside "last 24 h" on any day


class FakeX(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        q = parse_qs(urlsplit(self.path).query)
        SEEN.append(q)
        if self.headers.get("Authorization") != "Bearer t0k":
            return self.reply(401, {"title": "Unauthorized"})
        if q.get("query") == ["ratelimit"]:
            return self.reply(429, {}, {"x-rate-limit-reset": str(int(time.time()) + 600)})
        since = int(q.get("since_id", ["0"])[0])
        posts = [p for p in ({"id": "1900000000000000001", "author_id": "7", "text": "Nifty at record high",
                              "created_at": AGO},
                             {"id": "1900000000000000002", "author_id": "7", "text": "Sensex up 500",
                              "created_at": AGO}) if int(p["id"]) > since]
        self.reply(200, {"data": posts, "includes": {"users": [{"id": "7", "username": "marketdesk"}]}} if posts else {"meta": {"result_count": 0}})

    def reply(self, code, body, headers=None):
        b = json.dumps(body).encode()
        self.send_response(code)
        for k, v in {"Content-Type": "application/json", "Content-Length": str(len(b)), **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)


def serve(handler):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


fake = serve(FakeX)
os.environ["X_API_URL"] = f"http://127.0.0.1:{fake.server_port}/2/tweets/search/recent"
os.environ["NEWS_DB"] = os.path.join(tempfile.mkdtemp(), "news.db")
import news  # noqa: E402  (reads the two env vars above at import)
import scrape  # noqa: E402

c = scrape.Crawler(scrape.parse_args(["--db", news.DB]))   # creates the schema


def test_x_reader_stores_new_posts_once_and_respects_rate_limit():
    assert asyncio.run(news.poll_x(c.db, "t0k")) == news.EVERY
    rows = c.db.execute("SELECT url, title, published, summary FROM items ORDER BY url").fetchall()
    assert [r[0] for r in rows] == ["https://x.com/marketdesk/status/1900000000000000001",
                                    "https://x.com/marketdesk/status/1900000000000000002"], rows
    assert rows[0][3] == "@marketdesk" and "since_id" not in SEEN[-1]
    asyncio.run(news.poll_x(c.db, "t0k"))    # second call asks only for newer posts, stores nothing twice
    assert SEEN[-1]["since_id"] == ["1900000000000000002"]
    assert c.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert asyncio.run(news.poll_x(c.db, "wrong")) == 300.0 and "401" in news.STATE["x"]
    old = news.X_QUERY
    news.X_QUERY = "ratelimit"
    try:
        assert asyncio.run(news.poll_x(c.db, "t0k")) >= 590     # waits for the reset the API names
    finally:
        news.X_QUERY = old


def test_dashboard_api():
    web = serve(news.Web)
    base = f"http://127.0.0.1:{web.server_port}"
    get = lambda p: json.loads(urllib.request.urlopen(base + p).read())   # noqa: E731
    page = urllib.request.urlopen(base + "/").read()
    assert b"<title>News Wire</title>" in page
    its = get("/api/items")
    assert {i["source"] for i in its} == {"X"} and all(i["title"] for i in its)
    assert get(f"/api/items?since={max(i['seen'] for i in its)}") == []      # incremental refresh
    st = get("/api/stats")
    assert st["every"] == 20 and st["sources"]["X"]["day"] == 2
    try:
        urllib.request.urlopen(base + "/api/items?since=abc")
        raise AssertionError("bad input accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_news_sitemap_item_keeps_the_page_url_not_its_image():
    xml = (b'<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
           b'<url><loc>https://p.example/story-1</loc><news:news><news:title>ACME wins order</news:title>'
           b'<news:publication_date>2026-10-17T08:45:16+05:30</news:publication_date></news:news>'
           b'<image:image><image:loc>https://img.example/a.jpg</image:loc></image:image></url></urlset>')
    assert scrape.feed_items(xml) == [("https://p.example/story-1", "ACME wins order", "2026-10-17T08:45:16+05:30", None)]
