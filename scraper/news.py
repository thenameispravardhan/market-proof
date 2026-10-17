"""Local news collector + dashboard:  python news.py  ->  http://127.0.0.1:8765

Every 20 s, scrape.py polls news_feeds.txt: headlines only (title, link, time, summary), honest user agent,
robots.txt and per-source limits from news_sources.json, sources that refuse access are paused.
X (Twitter) is read only through the official API v2 recent search, and only when X_BEARER_TOKEN is set
(pay-per-use: each post returned is billed, so since_id + a daily cap, X_MAX_READS). Everything goes to news.db.
"""
import asyncio
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx

import scrape

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("NEWS_DB", os.path.join(HERE, "news.db"))
FEEDS = os.path.join(HERE, "news_feeds.txt")
SOURCES = os.path.join(HERE, "news_sources.json")
EVERY = 20                                   # seconds between poll starts (feeds and X)
PORT = int(os.environ.get("NEWS_PORT", "8765"))
X_URL = os.environ.get("X_API_URL", "https://api.x.com/2/tweets/search/recent")
X_QUERY = os.environ.get("X_QUERY", "(Sensex OR Nifty OR NSE OR BSE OR stock market India) lang:en -is:retweet")
X_MAX_READS = int(os.environ.get("X_MAX_READS", "1000"))   # posts per 24 h
LABELS = {"www.moneycontrol.com": "Moneycontrol", "feeds.feedburner.com": "NDTV", "www.zeebiz.com": "Zee Business",
          "www.cnbctv18.com": "CNBC-TV18", "economictimes.indiatimes.com": "Economic Times", "www.livemint.com": "Mint",
          "www.thehindubusinessline.com": "BusinessLine", "www.bseindia.com": "BSE", urlsplit(X_URL).netloc: "X"}
STATE = {"polls": 0, "last_poll": None, "poll_secs": None, "error": None, "x": "off (no X_BEARER_TOKEN)"}


def label(url):
    host = urlsplit(url).netloc
    return LABELS.get(host, host)


async def poll_x(db, token):
    """One recent-search call for posts newer than the newest stored. Returns seconds to wait before the next."""
    used = db.execute("SELECT COUNT(*) FROM items WHERE feed=? AND first_seen>?", (X_URL, time.time() - 86_400)).fetchone()[0]
    if used >= X_MAX_READS:
        STATE["x"] = f"daily cap reached ({used} posts / 24 h)"
        return EVERY
    last = db.execute("SELECT MAX(CAST(substr(url, instr(url, '/status/') + 8) AS INTEGER)) FROM items WHERE feed=?",
                      (X_URL,)).fetchone()[0]
    params = {"query": X_QUERY, "max_results": 10, "tweet.fields": "created_at", "expansions": "author_id",
              "user.fields": "username", **({"since_id": str(last)} if last else {})}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(X_URL, params=params, headers={"Authorization": f"Bearer {token}", "User-Agent": scrape.parse_args([]).ua})
    if r.status_code == 429:                 # wait for the window the API names, never retry early
        wait = max(float(r.headers.get("x-rate-limit-reset", 0)) - time.time(), 60.0)
        STATE["x"] = f"rate limited, next call in {wait:.0f}s"
        return wait
    if r.status_code >= 400:
        STATE["x"] = f"HTTP {r.status_code}: {r.text[:200]}"
        return 300.0 if r.status_code in (401, 402, 403) else EVERY
    d = r.json()
    users = {u["id"]: u["username"] for u in d.get("includes", {}).get("users", [])}
    rows = [(f"https://x.com/{users.get(t.get('author_id'), 'i')}/status/{t['id']}", X_URL, t.get("text", "")[:1000],
             t.get("created_at"), "@" + users.get(t.get("author_id"), "?"), time.time()) for t in d.get("data", [])]
    n = db.executemany("INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)", rows).rowcount
    STATE["x"] = f"ok, {n} new posts at {time.strftime('%H:%M:%S')}"
    return EVERY


async def collect():
    feeds = [ln.strip() for ln in open(FEEDS, encoding="utf8") if ln.strip() and not ln.startswith("#")]
    argv = ["--db", DB, "--sources", SOURCES, "--workers", "2", "--max-delay", "15"]
    token, hosts, x_at = os.environ.get("X_BEARER_TOKEN"), {}, 0.0
    while True:
        t0 = time.time()
        if STATE["polls"] % 180 == 0:        # ponytail: robots.txt re-read hourly by dropping host state; per-host TTL if it matters
            hosts = {}
        c = scrape.Crawler(scrape.parse_args(argv))
        c.hosts = hosts                      # politeness gaps, robots.txt, breakers and pauses carry over between polls
        try:
            await c.run(feeds)
            if token and time.time() >= x_at:
                x_at = time.time() + await poll_x(c.db, token)
            STATE["error"] = None
        except Exception as e:               # one bad poll never stops the collector
            STATE["error"] = repr(e)[:300]
        finally:
            c.db.close()
        STATE.update(polls=STATE["polls"] + 1, last_poll=time.time(), poll_secs=round(time.time() - t0, 1))
        await asyncio.sleep(max(0.0, EVERY - (time.time() - t0)))


def items(q):
    since = float(q.get("since", ["0"])[0])
    limit = max(1, min(int(q.get("limit", ["500"])[0]), 2000))
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT url, feed, title, published, summary, first_seen FROM items WHERE first_seen > ? "
                          "ORDER BY first_seen DESC LIMIT ?", (since, limit)).fetchall()
    finally:
        db.close()
    return [{"url": u, "source": label(f), "title": t, "published": p, "summary": s, "seen": seen}
            for u, f, t, p, s, seen in rows]


def stats():
    now = time.time()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        # by the story's own published time (first_seen is when *we* saw it: a first run would count everything as new)
        at = "COALESCE(CAST(strftime('%s', published) AS REAL), first_seen)"
        counts = db.execute(f"SELECT feed, COUNT(*), SUM({at} > ?), SUM({at} > ?), MAX(first_seen) FROM items "
                            "GROUP BY feed", (now - 3600, now - 86_400)).fetchall()
        last = db.execute("SELECT host, status, http, error, MAX(t) FROM events GROUP BY host").fetchall()
        paused = dict(db.execute("SELECT host, MAX(until) FROM pauses GROUP BY host HAVING MAX(until) > ?", (now,)).fetchall())
    finally:
        db.close()
    src = {}
    for feed, total, h1, h24, seen in counts:
        s = src.setdefault(label(feed), {"total": 0, "hour": 0, "day": 0, "last_seen": 0, "state": "ok"})
        s["total"] += total
        s["hour"] += h1 or 0
        s["day"] += h24 or 0
        s["last_seen"] = max(s["last_seen"], seen or 0)
    for host, st, http, err, t in last:
        state = "paused" if host in paused else scrape.liveness(st, http, err) or "ok"
        s = src.setdefault(LABELS.get(host, host), {"total": 0, "hour": 0, "day": 0, "last_seen": 0, "state": "ok"})
        s["state"], s["detail"] = state, (f"paused until {scrape.iso(paused[host])}" if host in paused else err or "")
    return {"sources": src, "collector": STATE, "every": EVERY, "now": now}


class Web(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        try:
            if u.path == "/":
                return self.send(200, open(os.path.join(HERE, "news.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/api/items":
                return self.send(200, items(parse_qs(u.query)))
            if u.path == "/api/stats":
                return self.send(200, stats())
            self.send(404, {"error": "not found"})
        except ValueError as e:              # bad since/limit
            self.send(400, {"error": str(e)})
        except sqlite3.OperationalError as e:   # DB not created yet
            self.send(503, {"error": str(e)})


if __name__ == "__main__":
    scrape.Crawler(scrape.parse_args(["--db", DB])).db.close()   # create the tables before the first request
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Web)      # local only
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"dashboard: http://127.0.0.1:{PORT}   (polling {FEEDS} every {EVERY}s; X: {'on' if os.environ.get('X_BEARER_TOKEN') else 'off'})", flush=True)
    asyncio.run(collect())
