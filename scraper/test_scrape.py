"""Self-check against local servers: python test_scrape.py"""
import asyncio
import base64
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import scrape


def make_pdf(text, pad=0):
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    if pad:  # an unreferenced filler stream, to make a valid PDF bigger than the HTML cap
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (pad, b"0" * pad))
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, o)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


MARKERS = {"text": "widgets", "author": "Ravi Kumar", "table": "TBL4242", "json-ld": "1234.56", "image": "/img/chart.png",
           "video": "/media/clip.mp4", "audio": "/media/talk.mp3", "pdf link": "/doc.pdf", "embed": "youtube.com/embed/xyz",
           "pdf text": "PDFMARK", "chart data (XHR)": "CHARTVAL777"}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PARA = "<p>" + "Quarterly revenue at ACME rose sharply as demand for widgets grew across every region. " * 6 + "</p>"
ARTICLE = f"""<html><head><title>ACME results</title><meta name="author" content="Ravi Kumar">
<meta property="article:published_time" content="2026-10-01T09:15:42+05:30">
<script type="application/ld+json">{{"@type": "Product", "name": "Widget", "offers": {{"price": "1234.56"}},}}</script></head>
<body><nav>Home | About</nav><article><h1>ACME results</h1>{PARA * 3}
<table><caption>Results</caption><tr><th>Quarter</th><th colspan="2">Revenue</th></tr>
<tr><td>Q2</td><td>TBL4242</td><td>45.6<sup class="footnote">2</sup></td></tr></table>{PARA}
<img src="/img/chart.png" alt="Revenue chart"><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" srcset="/img/small.png 150w, /img/chart.png 2000w">
<video src="/media/clip.mp4"></video><audio><source src="/media/talk.mp3"></audio>
<a href="/doc.pdf">Annual report</a><iframe src="https://www.youtube.com/embed/xyz"></iframe></article>
<a href='/c'>c</a><a href='/private/x'>p</a><a href='https://elsewhere.test/'>ext</a></body></html>"""
JS_PAGE = f"<html><head><title>JS</title></head><body><div id=app></div><script>document.getElementById('app').innerHTML = {json.dumps('<article>' + PARA * 4 + '</article>')}</script></body></html>"
CHART_PAGE = f"""<html><body><article><h1>Revenue chart</h1>{PARA * 3}</article><script>
for (let i = 0; i < 60; i++) fetch('/pad/' + i);
fetch('/api/chart.json').then(r => r.json()).then(d => {{ document.title = d.label; }});</script></body></html>"""
BLOCK_PAGE = "<html><head><title>Just a moment...</title></head><body>Verify you are human. Please enable JavaScript and cookies to continue.</body></html>"
NOINDEX = f'<html><head><title>Secret</title><meta name="robots" content="noindex,nofollow"></head><body><article><h1>Hidden</h1>{PARA * 3}</article></body></html>'
VARIANT = lambda tag: ARTICLE.replace("</article>", f"<p>Variant {tag} of the ACME story, with its own closing paragraph.</p></article>")  # noqa: E731 (distinct text: identical pages are now duplicates)
PAYWALL = f'<html><head><title>Sub</title><script type="application/ld+json">{{"@type":"Article","isAccessibleForFree":false}}</script></head><body><article><h1>Paid</h1>{PARA * 3}</article></body></html>'
FILES = {"/img/chart.png": (PNG, "image/png"), "/media/clip.mp4": (b"\0\0\0\x18ftypmp42", "video/mp4"),
         "/media/talk.mp3": (b"ID3\x03\0\0\0", "audio/mpeg"), "/doc.pdf": (make_pdf("PDFMARK annual report"), "application/pdf"),
         "/api/chart.json": (json.dumps({"label": "CHARTVAL777", "values": [3, 1, 4]}).encode(), "application/json")}
BIG_PDF = make_pdf("BIGPDFMARK", pad=5_000_000)  # over the 4 MB HTML cap, under the 25 MB file cap
hits = Counter()
CONC = {"now": 0, "max": 0, "starts": []}   # in-flight /s/ requests across every H server
LOCK = threading.Lock()
LM = "Wed, 01 Oct 2026 09:00:00 GMT"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body=b"", ctype="text/html", **headers):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        hits[self.path] += 1
        base = f"http://{self.headers['Host']}"
        p = self.path
        if p == "/robots.txt":
            return self.send(200, "User-agent: *\nDisallow: /private\n", "text/plain")
        if p == "/sitemap.xml":
            locs = "".join(f"<url><loc>{base}{x}</loc></url>"
                            for x in ("/a", "/b", "/js", "/c", "/block", "/noindex", "/paywall", "/red"))
            locs += "<url><loc>http://offhost.example/x</loc></url>"   # feed link to a host no seed named
            locs += f"<url><loc>{base}/q?x=1&amp;y=2</loc></url>"        # escaped & must be unescaped
            return self.send(200, f'<?xml version="1.0"?><urlset>{locs}</urlset>', "application/xml")
        if p == "/a":
            if self.headers.get("If-None-Match") == '"v1"':
                return self.send(304)
            return self.send(200, ARTICLE, ETag='"v1"')
        if p == "/b":
            return self.send(429, Retry_After="0") if hits["/b"] == 1 else self.send(200, VARIANT("b"))
        if p == "/c":
            return self.send(200, "<html><body><div id=root></div></body></html>")
        if p == "/red":
            return self.send(302, Location=f"{base}/a")
        if p in ("/js", "/chart"):
            return self.send(200, JS_PAGE if p == "/js" else CHART_PAGE)
        if p == "/block":
            return self.send(200, BLOCK_PAGE)
        if p == "/noindex":
            return self.send(200, NOINDEX)
        if p == "/urls.txt":
            return self.send(200, f"{base}/a\n{base}/js\n", "text/plain")
        if p == "/index.xml":
            return self.send(200, f'<?xml version="1.0"?><sitemapindex><sitemap><loc>{base}/sitemap.xml</loc>'
                                  f'</sitemap></sitemapindex>', "application/xml")
        if p == "/q?x=1&y=2":
            return self.send(200, VARIANT("q"))
        if p == "/big.pdf":
            return self.send(200, BIG_PDF, "application/pdf")
        if p == "/paywall":
            return self.send(200, PAYWALL)
        if p == "/many.xml":
            locs = "".join(f"<url><loc>{base}/s/{i}</loc></url>" for i in range(30))
            return self.send(200, f'<?xml version="1.0"?><urlset>{locs}</urlset>', "application/xml")
        if p.startswith("/s/"):              # slow page: lets a test see concurrency and kill a run mid-flight
            with LOCK:
                CONC["now"] += 1
                CONC["max"] = max(CONC["max"], CONC["now"])
                CONC["starts"].append(time.monotonic())
            time.sleep(0.15)
            with LOCK:
                CONC["now"] -= 1
            return self.send(200, VARIANT(p))
        if p.startswith("/dup/"):
            return self.send(200, VARIANT("dup"))
        if p == "/lm":
            if self.headers.get("If-Modified-Since") == LM:
                return self.send(304)
            return self.send(200, VARIANT("lm"), Last_Modified=LM)
        if p == "/pw-etag":
            if self.headers.get("If-None-Match") == '"pw"':
                return self.send(304)
            return self.send(200, PAYWALL, ETag='"pw"')
        if p.startswith("/news/"):           # published 90 s ago -> pub->ingest latency ~90 s
            pub = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 90))
            return self.send(200, ARTICLE.replace("2026-10-01T09:15:42+05:30", pub)
                             .replace("</article>", f"<p>Story {p} has its own closing line.</p></article>"))
        if p.startswith("/pad/"):
            return self.send(200, "x", "text/plain")
        if p in FILES:
            return self.send(200, *FILES[p])
        self.send(404)

    def do_POST(self):  # fake OpenAI-compatible LLM
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send(200, json.dumps({"choices": [{"message": {"content": '{"company": "ACME"}'}}]}), "application/json")


class Robots503(H):
    healthy = False  # flips to True to test --retry-failed

    def do_GET(self):
        if self.path == "/robots.txt":
            return self.send(200 if Robots503.healthy else 503, "", "text/plain")
        if self.path == "/p":
            return self.send(200, VARIANT("p"))
        self.send(404)


class Refuses(H):  # /slow, /slowdate: 429 with a Retry-After longer than --max-delay; anything else: 403
    def do_GET(self):
        from email.utils import formatdate
        hits["refuse" + self.path] += 1
        if self.path == "/robots.txt":
            return self.send(404)
        if self.path == "/slowdate":     # same pause, HTTP-date form
            return self.send(429, Retry_After=formatdate(time.time() + 3600, usegmt=True))
        return self.send(429, Retry_After="3600") if self.path == "/slow" else self.send(403)


class Proxy(BaseHTTPRequestHandler):  # minimal forward proxy for http:// targets (no redirects followed)
    def log_message(self, *a):
        pass

    def do_GET(self):
        import http.client
        from urllib.parse import urlsplit
        hits["proxy"] += 1
        u = urlsplit(self.path)
        c = http.client.HTTPConnection(u.netloc, timeout=10)
        try:
            c.request(self.command, u.path + (f"?{u.query}" if u.query else ""),
                      headers={k: v for k, v in self.headers.items() if k.lower() not in ("proxy-connection", "connection")})
            r = c.getresponse()
            body = r.read()
        except OSError:                      # origin down: a real proxy answers 502, it doesn't vanish
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in ("transfer-encoding", "connection"):
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET

    def do_CONNECT(self):  # https tunnel: only the origin-down case is needed, so always 502
        self.send_response(502)
        self.end_headers()


class Proxy407(Proxy):  # proxy up, our credentials wrong
    def do_GET(self):
        self.send_response(407)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_CONNECT = do_GET


class Server(ThreadingHTTPServer):
    request_queue_size = 128  # stdlib default 5 drops browser connections -> 1 s SYN retries on Windows


def start(handler=H):
    srv = Server(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", srv


def start_server():  # used by bench/coverage.py
    return start()[0]


def run(argv, seeds):
    asyncio.run(scrape.Crawler(scrape.parse_args(argv)).run(seeds))


def main():
    base, _ = start()
    d503, _ = start(Robots503)
    os.environ["LLM_BASE_URL"] = base
    os.environ["LLM_API_KEY"] = "test"
    tmp = tempfile.mkdtemp()
    schema = os.path.join(tmp, "s.json")
    json.dump({"company": "company name"}, open(schema, "w"))
    db, out = os.path.join(tmp, "t.db"), os.path.join(tmp, "files")
    argv = [f"{base}/sitemap.xml", f"{d503}/p", f"{base}/big.pdf", "--db", db, "--follow", "--delay", "0", "--browser", "--allow-private",
            "--schema", schema, "--download", "image,pdf,audio", "--out", out]
    run(argv, [f"{base}/sitemap.xml", f"{d503}/p", f"{base}/big.pdf"])

    con = scrape.sqlite3.connect(db)
    con.row_factory = scrape.sqlite3.Row
    rows = {r["url"].replace(base, "").replace(d503, "D"): r for r in con.execute("SELECT * FROM pages")}
    assets = {r["url"].replace(base, ""): r for r in con.execute("SELECT * FROM assets")}
    st = {k: r["status"] for k, r in rows.items()}

    a = rows["/a"]
    assert a["status"] == "done" and "ACME" in a["text"] and json.loads(a["data"]) == {"company": "ACME"}
    assert json.loads(a["meta"])["author"] == "Ravi Kumar", a["meta"]
    assert a["date"].startswith("2026-10-01T09:15:42"), a["date"]              # full timestamp kept (audit #32)
    tbls = json.loads(a["tables"])
    assert tbls[0]["caption"] == "Results", tbls                               # caption kept (audit #26)
    assert tbls[0]["rows"] == [["Quarter", "Revenue", ""], ["Q2", "TBL4242", "45.6"]], tbls  # colspan + sup
    assert json.loads(a["jsonld"])[0]["offers"]["price"] == "1234.56", a["jsonld"]   # trailing comma tolerated
    assert a["attempts"] == 1 and a["final_url"] == a["url"]

    kinds = {k: v["kind"] for k, v in assets.items()}
    assert kinds.get("/img/chart.png") == "image" and kinds.get("/doc.pdf") == "pdf"
    assert "https://www.youtube.com/embed/xyz" in {v["url"] for v in assets.values()}
    assert "/img/small.png" not in kinds and open(assets["/img/chart.png"]["path"], "rb").read() == PNG  # widest srcset, no data: URI
    assert assets["/doc.pdf"]["status"] == "done" and assets["/media/talk.mp3"]["status"] == "done"
    assert assets["/media/clip.mp4"]["status"] == "found" and hits["/media/clip.mp4"] == 0  # video not requested
    assert hits["/img/chart.png"] == 1                                         # one page references it: one fetch

    assert st["/doc.pdf"] == "done" and "PDFMARK" in rows["/doc.pdf"]["text"]   # followed PDF -> text
    assert st["/b"] == "done" and hits["/b"] == 2                               # retried after 429
    assert st["/c"] == "empty"                                                  # no text, render also empty
    assert st["/js"] == "done" and "widgets" in rows["/js"]["text"]             # rendered by Playwright
    assert st["/block"] == "blocked" and rows["/block"]["text"] is None         # challenge page, not a fake 'done'
    assert st["/noindex"] == "optout" and rows["/noindex"]["text"] is None      # meta robots noindex honoured
    assert st["/paywall"] == "paywalled"                                        # isAccessibleForFree=false
    assert st["/red"] == "redirect" and rows["/red"]["final_url"] == rows["/a"]["url"]  # redirect routed, not stored as /red
    assert st["/private/x"] == "blocked" and hits["/private/x"] == 0            # robots.txt
    assert st["D/p"] == "deferred"                                             # robots.txt 503 -> retryable, not blocked
    assert st["/big.pdf"] == "done" and "BIGPDFMARK" in rows["/big.pdf"]["text"]  # 5 MB PDF: PDF cap, not HTML cap
    assert "PDFMARK" in (assets["/doc.pdf"]["text"] or "")                     # downloaded PDF gets text too
    assert not any("offhost" in u for u in rows)                               # feed links stay on seed hosts
    assert st.get("/q?x=1&y=2") == "done"                                      # &amp; unescaped
    assert rows["/a"]["depth"] == 1 and rows["/doc.pdf"]["depth"] == 2         # feed children count toward --depth
    assert not any("elsewhere" in u for u in rows)                             # stayed on host

    # --refresh: /a 304 keeps 'done'; an 'empty' row must NOT flip to 'done' (audit #6)
    run(argv + ["--refresh"], [])
    con2 = scrape.sqlite3.connect(db)
    r_a = con2.execute("SELECT http, status FROM pages WHERE url=?", (base + "/a",)).fetchone()
    assert r_a == (304, "done"), r_a
    assert con2.execute("SELECT status, text FROM pages WHERE url=?", (base + "/c",)).fetchone() == ("empty", None)

    # --retry-failed re-queues the deferred robots-503 page
    assert con2.execute("SELECT attempts FROM pages WHERE url=?", (base + "/a",)).fetchone()[0] == 2

    # schema migration: an old DB missing a column opens and crawls without error
    old = os.path.join(tmp, "old.db")
    oc = scrape.sqlite3.connect(old)
    oc.execute("CREATE TABLE pages(url TEXT PRIMARY KEY, depth INT, status TEXT DEFAULT 'queued')")
    oc.close()
    run([f"{base}/a", "--db", old, "--delay", "0", "--allow-private"], [f"{base}/a"])
    assert scrape.sqlite3.connect(old).execute("SELECT status FROM pages WHERE url=?", (base + "/a",)).fetchone()[0] == "done"

    # --xhr keeps the same-host chart JSON even though 60 other responses arrive first (audit #12)
    xdb = os.path.join(tmp, "x.db")
    run([f"{base}/chart", "--db", xdb, "--delay", "0", "--xhr", "--allow-private"], [f"{base}/chart"])
    xhr = json.loads(scrape.sqlite3.connect(xdb).execute("SELECT xhr FROM pages WHERE url=?", (base + "/chart",)).fetchone()[0])
    assert any(g["body"].get("label") == "CHARTVAL777" for g in xhr), xhr

    # --retry-failed: the robots-503 host recovers, its deferred page is fetched
    Robots503.healthy = True
    run(["--db", db, "--delay", "0", "--allow-private", "--retry-failed"], [])
    assert scrape.sqlite3.connect(db).execute("SELECT status FROM pages WHERE url=?", (d503 + "/p",)).fetchone()[0] == "done"

    # SSRF guard: without --allow-private a loopback URL is refused and never requested
    before = hits["/a"]
    sdb = os.path.join(tmp, "ssrf.db")
    run([f"{base}/a", "--db", sdb, "--delay", "0"], [f"{base}/a"])
    st_, err_ = scrape.sqlite3.connect(sdb).execute("SELECT status, error FROM pages").fetchone()
    assert st_ == "failed" and "SSRF" in err_ and hits["/a"] == before, (st_, err_)

    # circuit breaker: a host refusing connections gets BREAKER tries, the rest are deferred
    import socket
    s_ = socket.socket()
    s_.bind(("127.0.0.1", 0))
    dead = f"http://127.0.0.1:{s_.getsockname()[1]}"
    s_.close()
    bdb = os.path.join(tmp, "dead.db")
    run(["--db", bdb, "--delay", "0", "--allow-private", "--per-host", "1", "--ignore-robots"], [f"{dead}/{i}" for i in range(12)])
    sts = Counter(x for (x,) in scrape.sqlite3.connect(bdb).execute("SELECT status FROM pages"))
    assert sts["failed"] <= scrape.BREAKER + 1 and sts["deferred"] >= 12 - scrape.BREAKER - 1, sts
    assert scrape.sqlite3.connect(bdb).execute("SELECT COUNT(*) FROM pages WHERE error LIKE 'circuit open%'").fetchone()[0] >= 1

    # a host answering 403 to everything gets BREAKER tries, then its other URLs are parked, not hammered
    ref, _ = start(Refuses)
    rdb = os.path.join(tmp, "ref.db")
    run(["--db", rdb, "--delay", "0", "--allow-private", "--per-host", "1"], [f"{ref}/{i}" for i in range(12)])
    sts = Counter(x for (x,) in scrape.sqlite3.connect(rdb).execute("SELECT status FROM pages"))
    tries = sum(v for k, v in hits.items() if k.startswith("refuse/") and k != "refuse/robots.txt")
    assert tries <= scrape.BREAKER + 1 and sts["deferred"] >= 12 - scrape.BREAKER - 1, (tries, sts)
    # Retry-After 3600 > --max-delay: one request, no early retry, host parked
    sdb2 = os.path.join(tmp, "slow.db")
    run(["--db", sdb2, "--delay", "0", "--allow-private", "--max-delay", "5", "--per-host", "1"], [f"{ref}/slow", f"{ref}/after"])
    assert hits["refuse/slow"] == 1 and hits["refuse/after"] == 0, hits
    assert dict(scrape.sqlite3.connect(sdb2).execute("SELECT url, status FROM pages"))[f"{ref}/after"] == "deferred"

    # --proxy failover: a dead proxy is skipped at start, the crawl goes through the next one
    prx, _ = start(Proxy)
    pdb = os.path.join(tmp, "proxy.db")
    run(["--db", pdb, "--delay", "0", "--allow-private", "--proxy", dead, "--proxy", prx], [f"{base}/a"])
    assert hits["proxy"] >= 3, hits["proxy"]                                   # probe + robots + page
    assert scrape.sqlite3.connect(pdb).execute("SELECT status FROM pages").fetchone()[0] == "done"
    try:                                                                       # every proxy dead: stop, no silent direct
        run(["--db", os.path.join(tmp, "p2.db"), "--allow-private", "--proxy", dead], [f"{base}/a"])
        raise AssertionError("ran without a working proxy")
    except SystemExit as e:
        assert "no --proxy answered" in str(e)

    # --report: the 403 host is DOWN (exit 1), a healthy DB reports nothing down
    down = scrape.Crawler(scrape.parse_args(["--db", rdb])).report()
    assert down == [ref.split("//")[1]], down
    assert scrape.Crawler(scrape.parse_args(["--db", pdb])).report() == []

    # sitemap index -> sitemap -> pages: each hop counts toward --depth
    idb = os.path.join(tmp, "idx.db")
    run(["--db", idb, "--delay", "0", "--allow-private", "--depth", "1"], [f"{base}/index.xml"])
    got = dict(scrape.sqlite3.connect(idb).execute("SELECT url, depth FROM pages"))
    assert got.get(base + "/sitemap.xml") == 1 and base + "/a" not in got, got   # /a would be depth 2 > 1

    # plain-text sitemap: one URL per line
    tdb = os.path.join(tmp, "txt.db")
    run(["--db", tdb, "--delay", "0", "--allow-private"], [f"{base}/urls.txt"])
    got = dict(scrape.sqlite3.connect(tdb).execute("SELECT url, status FROM pages"))
    assert got.get(base + "/a") == "done" and base + "/js" in got, got

    # --ignore-optout keeps the text of a noindex page and flags it
    odb = os.path.join(tmp, "opt.db")
    run([f"{base}/noindex", "--db", odb, "--delay", "0", "--allow-private", "--ignore-optout"], [f"{base}/noindex"])
    st_, meta_ = scrape.sqlite3.connect(odb).execute("SELECT status, meta FROM pages").fetchone()
    assert st_ == "done" and json.loads(meta_)["optout"] is True, (st_, meta_)

    # --forget deletes rows and unlinks files no surviving page references
    img_path = assets["/img/chart.png"]["path"]
    fc = scrape.Crawler(scrape.parse_args(["--db", db, "--allow-private"]))
    fc.forget(base + "/b")
    assert os.path.exists(img_path)       # /a still references it
    fc.forget(base + "/q")                # /q serves the same article, so it references the image too
    fc.forget(base + "/a")
    assert fc.db.execute("SELECT COUNT(*) FROM pages WHERE url=?", (base + "/a",)).fetchone()[0] == 0
    assert not os.path.exists(img_path)   # now unreferenced -> unlinked

    regressions(base, dead, prx, tmp)
    print("ok")


def report(db):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bad = scrape.Crawler(scrape.parse_args(["--db", db])).report()
    return bad, buf.getvalue()


def free_port():
    import socket
    s_ = socket.socket()
    s_.bind(("127.0.0.1", 0))
    port = s_.getsockname()[1]
    s_.close()
    return port


def regressions(base, dead, prx, tmp):
    from email.utils import formatdate
    q = lambda db, sql, *a: scrape.sqlite3.connect(db).execute(sql, a).fetchall()  # noqa: E731
    host = base.split("//")[1]

    # 1. Retry-After: seconds and all three HTTP-date forms; zone-less dates are GMT, not local time
    now = time.time()
    near = lambda v: abs(scrape.retry_after(v) - 120) < 3  # noqa: E731
    gm = time.gmtime(now + 120)
    assert scrape.retry_after("120") == 120 and scrape.retry_after(" 120 ") == 120
    assert near(formatdate(now + 120, usegmt=True))                            # IMF-fixdate
    assert near(time.strftime("%A, %d-%b-%y %H:%M:%S GMT", gm))                # RFC 850
    assert near(time.strftime("%a %b %d %H:%M:%S %Y", gm))                     # asctime: was 0.0 on an IST clock
    assert near(formatdate(now + 120).replace("+0000", "-0000"))               # -0000: was 0.0 too
    assert scrape.retry_after(formatdate(now - 999, usegmt=True)) == 0         # past date: retry now
    for junk in ("", "-5", "1.5", "soon", "Wed, 21 Oct 99999 07:28:00 GMT"):
        assert scrape.retry_after(junk) is None, junk
    ref, _ = start(Refuses)                                                    # HTTP-date pause > --max-delay: parked
    rdb = os.path.join(tmp, "slowdate.db")
    run(["--db", rdb, "--delay", "0", "--allow-private", "--max-delay", "5", "--per-host", "1"], [f"{ref}/slowdate", f"{ref}/later"])
    assert hits["refuse/slowdate"] == 1 and hits["refuse/later"] == 0, hits
    assert dict(q(rdb, "SELECT url, status FROM pages"))[f"{ref}/later"] == "deferred"

    # 2a. resume: a stopped run's discovered-but-unfetched links (status NULL) are picked up next run
    sdb = os.path.join(tmp, "stop.db")
    run(["--db", sdb, "--delay", "0", "--allow-private", "--max-pages", "3"], [f"{base}/sitemap.xml"])
    assert q(sdb, "SELECT COUNT(*) FROM pages WHERE status IS NULL")[0][0] > 0
    run(["--db", sdb, "--delay", "0", "--allow-private"], [])
    assert q(sdb, "SELECT COUNT(*) FROM pages WHERE status IS NULL")[0][0] == 0   # was: never fetched, silently

    # 2b. hard kill mid-crawl (TerminateProcess / SIGKILL), fresh process resumes; done pages aren't refetched
    kdb = os.path.join(tmp, "kill.db")
    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.Popen([sys.executable, "scrape.py", f"{base}/many.xml", "--db", kdb, "--delay", "0",
                             "--allow-private", "--per-host", "2"], cwd=here, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    n, deadline = 0, time.time() + 60
    while n < 4 and time.time() < deadline:
        time.sleep(0.05)
        try:
            n = q(kdb, "SELECT COUNT(*) FROM pages WHERE status='done' AND url LIKE '%/s/%'")[0][0]
        except scrape.sqlite3.Error:
            pass                             # DB not created yet
    proc.kill()
    proc.wait()
    before = {u for (u,) in q(kdb, "SELECT url FROM pages WHERE status='done' AND url LIKE '%/s/%'")}
    assert 4 <= len(before) < 30, len(before)
    run(["--db", kdb, "--delay", "0", "--allow-private", "--per-host", "4"], [])
    assert q(kdb, "PRAGMA integrity_check")[0][0] == "ok"
    assert q(kdb, "SELECT COUNT(*) FROM pages WHERE status='done' AND url LIKE '%/s/%'")[0][0] == 30
    assert all(hits[u.replace(base, "")] == 1 for u in before), [hits[u.replace(base, "")] for u in before]

    # 2c. DB write-locked by another connection (e.g. the bot reading it): the run waits, then works
    lock = scrape.sqlite3.connect(kdb, isolation_level=None, check_same_thread=False)
    lock.execute("BEGIN IMMEDIATE")
    threading.Timer(1.5, lambda: lock.execute("COMMIT")).start()
    t0 = time.monotonic()
    run(["--db", kdb, "--delay", "0", "--allow-private"], [f"{base}/s/99"])
    assert time.monotonic() - t0 >= 1.2 and dict(q(kdb, "SELECT url, status FROM pages"))[f"{base}/s/99"] == "done"

    # 3. proxy health: proxy faults (dead TCP, 407) fail over; an origin fault through a live proxy does not
    origin_down = f"https://127.0.0.1:{free_port()}/robots.txt"
    p407, _ = start(Proxy407)

    def pick(*proxies, probe=origin_down):
        argv = ["--db", os.path.join(tmp, "pick.db")] + [x for p_ in proxies for x in ("--proxy", p_)]
        return asyncio.run(scrape.Crawler(scrape.parse_args(argv)).pick_proxy(probe))

    assert pick(dead, p407, prx) == prx                                        # both proxy faults skipped
    assert pick(prx) == prx                                                    # CONNECT 502: origin's fault (was: SystemExit)
    assert pick(prx, probe=f"http://127.0.0.1:{free_port()}/robots.txt") == prx   # http origin down -> proxy's 502
    assert pick(prx, probe=f"{base}/forbidden-anything") == prx               # origin 404: proxy kept
    assert pick(dead, "direct") is None                                        # explicit direct fallback
    try:
        pick(dead, p407)
        raise AssertionError("picked a broken proxy")
    except SystemExit:
        pass

    # 4a. concurrency: never more than --per-host in flight to one host, but it does run in parallel
    CONC.update(now=0, max=0, starts=[])
    run(["--db", os.path.join(tmp, "c1.db"), "--delay", "0", "--allow-private", "--per-host", "3"],
        [f"{base}/s/{i}" for i in range(200, 212)])
    assert 2 <= CONC["max"] <= 3, CONC["max"]
    base2, _ = start()                                                         # --concurrency caps the total across hosts
    CONC.update(now=0, max=0, starts=[])
    run(["--db", os.path.join(tmp, "c2.db"), "--delay", "0", "--allow-private", "--per-host", "4", "--concurrency", "3"],
        [f"{b}/s/{i}" for b in (base, base2) for i in range(300, 306)])
    assert CONC["max"] <= 3, CONC["max"]
    CONC.update(now=0, max=0, starts=[])                                       # --delay spaces request starts to one host
    run(["--db", os.path.join(tmp, "c3.db"), "--delay", "0.25", "--allow-private", "--per-host", "2"],
        [f"{base}/s/{i}" for i in range(400, 406)])
    st = sorted(CONC["starts"])
    gaps = [round(b - a, 3) for a, b in zip(st, st[1:])]
    assert len(st) == 6 and min(gaps) >= 0.2, gaps

    # 4b. duplicates: 6 identical pages fetched in parallel -> exactly 1 done, 5 duplicate pointing at it
    ddb = os.path.join(tmp, "dup.db")
    run(["--db", ddb, "--delay", "0", "--allow-private", "--per-host", "6"], [f"{base}/dup/{i}" for i in range(6)])
    rows = q(ddb, "SELECT url, status, final_url FROM pages")
    done = [u for u, s_, _ in rows if s_ == "done"]
    assert len(done) == 1 and [s_ for _, s_, _ in rows].count("duplicate") == 5, rows
    assert all(f == done[0] for _, s_, f in rows if s_ == "duplicate")
    assert q(ddb, "SELECT COUNT(*) FROM pages WHERE status='duplicate' AND text IS NOT NULL")[0][0] == 5   # copies kept too
    n_ = scrape.normalize                                                      # one row per URL, whatever its spelling
    assert n_("HTTP://Example.COM:80/a?utm_source=x&id=2#top") == n_("http://example.com/a?id=2") == "http://example.com/a?id=2"

    # 4c. conditional GET: Last-Modified -> If-Modified-Since -> 304 keeps the stored text
    cdb = os.path.join(tmp, "lm.db")
    run(["--db", cdb, "--delay", "0", "--allow-private"], [f"{base}/lm", f"{base}/pw-etag"])
    run(["--db", cdb, "--delay", "0", "--allow-private", "--refresh"], [])
    got = {u.replace(base, ""): (s_, h_, t_) for u, s_, h_, t_ in q(cdb, "SELECT url, status, http, text FROM pages")}
    assert got["/lm"][:2] == ("done", 304) and "Variant lm" in got["/lm"][2], got["/lm"]
    assert got["/pw-etag"][:2] == ("paywalled", 200), got["/pw-etag"]        # was: ('empty', 304)

    # 5 + 6. metrics and report: success rate, parse failures, pub->ingest latency; current vs history
    mdb = os.path.join(tmp, "metrics.db")
    run(["--db", mdb, "--delay", "0", "--allow-private"], [f"{base}/news/1", f"{base}/news/2", f"{base}/c", f"{base}/nope"])
    bad, out = report(mdb)
    line = next(ln for ln in out.splitlines() if host in ln and "success" in ln)
    assert bad == [] and "success  50% of 4" in line and "parse-fail 1 " in line and "failures 0 " in line, out
    p50 = int(re.search(r"p50/p90 (\d+)s", line)[1])
    assert 85 <= p50 <= 150, line                                              # published 90 s before ingest
    ing = q(mdb, "SELECT ingested FROM pages WHERE url=?", f"{base}/news/1")[0][0]
    run(["--db", mdb, "--delay", "0", "--allow-private", "--refresh"], [])
    assert q(mdb, "SELECT ingested FROM pages WHERE url=?", f"{base}/news/1")[0][0] == ing   # re-fetch keeps first ingest
    Robots503.healthy = False                                                  # source down, then recovered
    d, _ = start(Robots503)
    fdb = os.path.join(tmp, "flap.db")
    run(["--db", fdb, "--delay", "0", "--allow-private"], [f"{d}/p"])
    bad, out = report(fdb)
    assert bad == [d.split("//")[1]] and "NETWORK" in out and "robots unreachable" in out, out
    Robots503.healthy = True
    run(["--db", fdb, "--delay", "0", "--allow-private", "--retry-failed"], [])
    bad, out = report(fdb)
    cur, hist = out.split("HISTORY")
    assert bad == [] and "OK" in cur and "NETWORK" not in cur, out               # current: recovered
    assert "failures 1 " in hist and "last failure" in hist and "robots unreachable" in hist, out   # history kept
    rdb2 = os.path.join(tmp, "ref2.db")                                        # restricting access: still paused
    run(["--db", rdb2, "--delay", "0", "--allow-private", "--per-host", "1"], [f"{ref}/x{i}" for i in range(9)])
    bad, out = report(rdb2)
    assert bad == [ref.split("//")[1]] and "RESTRICTED" in out and "access refusals" in out, out
    assert q(rdb2, "SELECT COUNT(*) FROM pages WHERE status='deferred'")[0][0] >= 9 - scrape.BREAKER - 1



# ---------------- pytest: python -m pytest test_scrape.py  (python test_scrape.py runs everything too) ----------------

def test_selfcheck():                        # the original end-to-end suite + regressions()
    main()


PH = Counter()                               # (port, path) -> requests, for the P servers below
MODE = {"down": False, "ra": "3600", "img": "", "fresh": False, "bse": 1}


class P(H):
    """Routes for the pytest cases. Every request is counted per server, robots.txt included."""
    def do_GET(self):
        port, path = self.server.server_port, self.path
        PH[port, path] += 1
        base = f"http://{self.headers['Host']}"
        if path == "/robots.txt":
            return self.send(404)
        if path == "/rss.xml":               # RSS 2.0: the URL is the <link> element's text
            if self.headers.get("If-None-Match") == '"r1"':
                return self.send(304)
            items = "".join(f"<item><title>{x}</title><link>{base}{x}</link></item>" for x in ("/p/a1", "/p/a2", "/x/other"))
            return self.send(200, f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>',
                             "application/rss+xml", ETag='"r1"')
        if path == "/news.rss":              # headlines: one old item, plus a fresh one (published 30 s ago) once MODE says so
            old = "Fri, 16 Oct 2026 03:00:00 GMT"
            new = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - 30))
            items = f"<item><title>Old story</title><link>{base}/p/old1</link><pubDate>{old}</pubDate></item>"
            if MODE["fresh"]:
                items += f"<item><title>ACME wins order</title><link>{base}/p/new1</link><pubDate>{new}</pubDate></item>"
            return self.send(200, f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>', "application/rss+xml")
        if path == "/bse.xml":               # BSE-style: pubDate in IST with no zone marker
            ist = time.strftime("%d-%b-%Y %H:%M:%S", time.gmtime(time.time() + 19800 - 30))   # 30 s ago, IST wall clock
            sets = {1: ["a1"], 2: ["a1", "b1"], 3: ["c1", "c2"]}[MODE["bse"]]   # 3: nothing overlaps poll 2 -> gap
            items = "".join(f"<item><title>{x}</title><link>{base}/p/b-{x}</link><pubDate>{'16-Oct-2026 09:00:00' if x == 'a1' else ist}"
                            f"</pubDate></item>" for x in sets)
            return self.send(200, f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>', "application/rss+xml")
        if path.startswith("/p/"):
            if MODE["down"]:
                return self.send(503)
            if self.headers.get("If-None-Match") == '"p"':
                return self.send(304)
            return self.send(200, VARIANT(path), ETag='"p"')
        if path.startswith("/x/"):
            return self.send(200, VARIANT(path))
        if path.startswith("/deny"):
            return self.send(403)
        if path.startswith("/ra"):
            return self.send(429, Retry_After=MODE["ra"])
        if path.startswith("/o"):
            return self.send(503)
        if path == "/withimg":               # an article whose image lives on another (maybe paused) host
            return self.send(200, VARIANT("img").replace("</article>", f'<img src="{MODE["img"]}"></article>'))
        if path == "/render":                # static: an app shell; the browser's request gets a 403 page
            if PH[port, path] > 1:
                return self.send(403, "<html><head><title>Access denied</title></head><body>" + "denied " * 200 + "</body></html>")
            return self.send(200, "<html><body><div id=app>loading</div></body></html>")
        if path == "/sebi":                     # SEBI-style wrapper: a title, the order is the PDF in a viewer iframe
            return self.send(200, f"<html><head><title>SEBI order</title></head><body><h1>SEBI order</h1>"
                                  f"<iframe src='/web/?file={base}/attach/o.pdf'></iframe></body></html>")
        if path == "/attach/o.pdf":
            return self.send(200, make_pdf("SEBIORDERMARK penalty of Rs 5 lakh"), "application/pdf")
        return self.send(404)


class RobotsToLocalhost(P):                  # robots.txt redirects to another (non-public) host
    target = ""

    def do_GET(self):
        if self.path == "/robots.txt":
            PH[self.server.server_port, "/robots.txt"] += 1
            return self.send(302, Location=self.target)
        return super().do_GET()


def srv(handler=P):
    base, s_ = start(handler)
    return base, s_.server_port


def crawl(db, seeds, *flags):
    run(["--db", db, "--delay", "0", "--allow-private", *flags], seeds)


def sent(port, prefix=""):
    return sum(n for (p_, path), n in PH.items() if p_ == port and path.startswith(prefix))


def rows(db, sql, *a):
    return scrape.sqlite3.connect(db).execute(sql, a).fetchall()


def sources(tmp_path, cfg):
    f = tmp_path / "sources.json"
    f.write_text(json.dumps(cfg))
    return ["--sources", str(f)]


def test_wrapper_page_takes_its_embedded_pdf_text(tmp_path):
    base, _ = srv()
    db = str(tmp_path / "w.db")
    crawl(db, [f"{base}/sebi"])
    st, text, meta = rows(db, "SELECT status, text, meta FROM pages WHERE url=?", f"{base}/sebi")[0]
    assert st == "done" and "SEBIORDERMARK" in text and "/attach/o.pdf" in meta    # was: 'empty' (title only)


def test_scanned_pdf_falls_back_to_ocr(monkeypatch):
    monkeypatch.setattr(scrape, "ocr_pdf", lambda body: "Recovery Certificate No. 6665 for Rs. 1,12,000")
    text, meta = scrape.pdf_text(make_pdf(""))          # no text layer, like a scanned SEBI notice
    assert text.startswith("Recovery Certificate") and meta["ocr"] is True
    assert scrape.pdf_text(make_pdf("TEXTLAYER"))[1].get("ocr") is None   # a real text layer: no OCR


def test_rss2_links_and_unchanged_feed_is_304(tmp_path):
    base, port = srv()
    db = str(tmp_path / "rss.db")
    crawl(db, [f"{base}/rss.xml"])
    crawl(db, [f"{base}/rss.xml"])           # second poll: the same feed, unchanged
    st = dict(rows(db, "SELECT url, status FROM pages"))
    assert st[f"{base}/p/a1"] == "done" and st[f"{base}/p/a2"] == "done"        # was: RSS 2.0 gave no links (D0)
    assert rows(db, "SELECT http FROM pages WHERE url=?", f"{base}/rss.xml")[0][0] == 304   # was: full 200 (D1)
    assert PH[port, "/p/a1"] == 1 and PH[port, "/rss.xml"] == 2               # old articles are not re-crawled


def test_failed_repoll_keeps_text_and_comes_back_via_304(tmp_path):
    base, _ = srv()
    db = str(tmp_path / "keep.db")
    crawl(db, [f"{base}/p/k"])
    MODE["down"] = True
    try:
        crawl(db, [f"{base}/p/k"], "--max-delay", "0")
        st, http, n = rows(db, "SELECT status, http, length(text) FROM pages")[0]
        assert (st, http) == ("failed", 503) and n > 300, (st, http, n)        # was: text NULLed on a 503
    finally:
        MODE["down"] = False
    crawl(db, [f"{base}/p/k"])
    assert rows(db, "SELECT status, http FROM pages")[0] == ("done", 304)


def test_final_attempt_retry_after_is_honoured(tmp_path):
    base, port = srv()
    MODE["ra"] = "2"
    try:
        t0 = time.monotonic()
        crawl(str(tmp_path / "ra.db"), [f"{base}/ra1", f"{base}/ra2"], "--per-host", "1",
              *sources(tmp_path, {f"127.0.0.1:{port}": {"retries": 0}}))
        assert PH[port, "/ra1"] == 1 and PH[port, "/ra2"] == 1                 # retries: 0 from the source config
        assert time.monotonic() - t0 >= 2                                       # was: next request 0.0 s later (D4)
    finally:
        MODE["ra"] = "3600"


def test_paused_source_gets_no_requests_next_run(tmp_path):
    base, port = srv()
    other, _ = srv()
    db = str(tmp_path / "pause.db")
    crawl(db, [f"{base}/deny{i}" for i in range(8)], "--per-host", "1")
    assert sent(port, "/deny") == scrape.BREAKER
    before = sent(port)
    MODE["img"] = f"{base}/deny-img.png"     # an asset on the paused host, referenced from a healthy one
    try:
        crawl(db, [f"{base}/deny-new{i}" for i in range(4)] + [f"{other}/withimg"], "--download", "image",
              "--out", str(tmp_path / "files"))
    finally:
        MODE["img"] = ""
    assert sent(port) == before                                                 # nothing at all: pages, robots, downloads
    assert rows(db, "SELECT status FROM assets WHERE url=?", f"{base}/deny-img.png")[0][0] == "found"   # kept for later
    errs = {e for (e,) in rows(db, "SELECT error FROM pages WHERE url LIKE ?", f"{base}/deny-new%")}
    err = errs.pop()
    assert not errs and err.startswith("paused until") and "(restricted:" in err, err
    # Retry-After 3600 > --max-delay: honoured by the next run, not just this one
    ra = str(tmp_path / "ra.db")
    crawl(ra, [f"{base}/ra-x"], "--max-delay", "5")
    n = PH[port, "/ra-x"]
    crawl(ra, [f"{base}/ra-x"], "--max-delay", "5")
    assert n == 1 and PH[port, "/ra-x"] == 1, PH[port, "/ra-x"]                 # was: requested again (D2)


def test_pause_expiry_probes_once_then_doubles(tmp_path):
    base, port = srv()
    db = str(tmp_path / "probe.db")
    crawl(db, [f"{base}/deny{i}" for i in range(6)], "--per-host", "1", "--pause", "1")
    time.sleep(1.2)                          # pause expired, nothing succeeded since
    before = sent(port, "/deny")
    crawl(db, [f"{base}/deny-p{i}" for i in range(6)], "--per-host", "4", "--pause", "1")
    assert sent(port, "/deny") - before == 1                                    # one probe, not 5 more refusals
    (d1,), (d2,) = rows(db, "SELECT until - start FROM pauses ORDER BY start")
    assert abs(d1 - 1) < 0.2 and abs(d2 - 2) < 0.2, (d1, d2)                    # second strike: twice as long
    asyncio.run(scrape.main(scrape.parse_args(["--db", db, "--resume", f"127.0.0.1:{port}"])))   # operator: access arranged
    assert rows(db, "SELECT MAX(until) FROM pauses")[0][0] <= time.time()


def test_outage_trips_breaker(tmp_path):
    base, port = srv()
    crawl(str(tmp_path / "o.db"), [f"{base}/o{i}" for i in range(20)], "--per-host", "1", "--max-delay", "0")
    assert sent(port, "/o") == scrape.BREAKER * 4, sent(port, "/o")              # was: 80 (4 per URL, every URL)


def test_browser_error_page_is_not_content(tmp_path):
    base, _ = srv()
    db = str(tmp_path / "render.db")
    crawl(db, [f"{base}/render"], "--browser")
    st, text = rows(db, "SELECT status, coalesce(text, '') FROM pages")[0]
    assert "denied" not in text, (st, text[:80])                                # was: the 403 page could be stored


def test_robots_redirect_to_private_host_is_refused(tmp_path, monkeypatch):
    target, tport = srv()
    RobotsToLocalhost.target = f"http://localhost:{tport}/robots.txt"
    base, _ = srv(RobotsToLocalhost)
    monkeypatch.setattr(scrape, "public_host", lambda h: h == "127.0.0.1")    # pretend the seed host is public
    c = scrape.Crawler(scrape.parse_args(["--db", str(tmp_path / "r.db")]))
    verdict = asyncio.run(c._load_robots(f"{base}/a"))
    assert "non-public" in verdict and PH[tport, "/robots.txt"] == 0, verdict  # was: followed to any host


def test_source_config_method_allow_ttl_budget(tmp_path):
    base, port = srv()
    key = f"127.0.0.1:{port}"
    db = str(tmp_path / "cfg.db")
    cfg = {key: {"method": "feed", "allow": [r"/p/a\d$"], "ttl": 3600, "access": "RSS, personal use"}}
    crawl(db, [f"{base}/rss.xml"], "--follow", *sources(tmp_path, cfg))
    got = {u.replace(base, "") for (u,) in rows(db, "SELECT url FROM pages")}
    assert got == {"/rss.xml", "/p/a1", "/p/a2"}, got                           # /x/other filtered by allow
    n = sent(port)
    crawl(db, [f"{base}/rss.xml"], *sources(tmp_path, cfg))                     # within ttl: no request at all
    assert sent(port) == n and rows(db, "SELECT COUNT(*) FROM events WHERE status='cached'")[0][0] == 1
    # method none: not requested, stored data untouched
    crawl(db, [f"{base}/p/a1"], *sources(tmp_path, {key: {"method": "none"}}))
    assert sent(port) == n and rows(db, "SELECT status FROM pages WHERE url=?", f"{base}/p/a1")[0][0] == "done"
    # budget: 3 requests per 24 h, counted across runs from the request log
    bdb = str(tmp_path / "budget.db")
    bcfg = sources(tmp_path, {key: {"budget": 3}})
    n = sent(port)
    crawl(bdb, [f"{base}/x/b{i}" for i in range(5)], "--ignore-robots", "--per-host", "1", *bcfg)
    crawl(bdb, [f"{base}/x/c{i}" for i in range(5)], "--ignore-robots", "--per-host", "1", *bcfg)
    assert sent(port) - n == 3, sent(port) - n
    assert rows(bdb, "SELECT kind FROM pauses")[0][0] == "budget"
    # delay from the source config spaces request starts
    CONC.update(now=0, max=0, starts=[])
    sbase, sport = srv(H)
    run(["--db", str(tmp_path / "d.db"), "--allow-private", *sources(tmp_path, {f"127.0.0.1:{sport}": {"delay": 0.3}})],
        [f"{sbase}/s/{i}" for i in range(500, 504)])
    st_ = sorted(CONC["starts"])
    assert min(b - a for a, b in zip(st_, st_[1:])) >= 0.25, st_


def test_headlines_store_feed_items_without_fetching_articles(tmp_path):
    base, port = srv()
    db = str(tmp_path / "h.db")
    cfg = sources(tmp_path, {f"127.0.0.1:{port}": {"method": "headlines"}})
    crawl(db, [f"{base}/news.rss"], *cfg)
    MODE["fresh"] = True
    try:
        time.sleep(1.1)                      # a later poll, so the new item is not part of the first-poll backlog
        crawl(db, [f"{base}/news.rss"], *cfg)
    finally:
        MODE["fresh"] = False
    items = {u.replace(base, ""): (t, pub) for u, t, pub in rows(db, "SELECT url, title, published FROM items")}
    assert set(items) == {"/p/old1", "/p/new1"} and items["/p/new1"][0] == "ACME wins order", items
    assert items["/p/old1"][1] == "2026-10-16T03:00:00+00:00", items             # RFC-822 -> ISO
    assert sent(port, "/p/") == 0                                                # no article fetched
    bad, out = report(db)
    line = next(ln for ln in out.splitlines() if "headlines" in ln)
    assert "headlines 2 (1h)" in line and "(n=1)" in line, line                  # backlog item excluded from latency
    assert 25 <= int(re.search(r"p50/p90 (\d+)s", line)[1]) <= 90, line


def test_feed_gap_zone_less_dates_and_backlog(tmp_path):
    base, port = srv()
    db = str(tmp_path / "bse.db")
    cfg = sources(tmp_path, {f"127.0.0.1:{port}": {"method": "feed", "tz": 5.5}})
    try:
        for step in (1, 2, 3):
            MODE["bse"] = step
            crawl(db, [f"{base}/bse.xml"], *cfg)
            time.sleep(1.1)                  # separate polls, so poll 1 is the only backlog
    finally:
        MODE["bse"] = 1
    errs = [e for (e,) in rows(db, "SELECT error FROM events WHERE status='done' AND error LIKE 'possible gap%'")]
    assert len(errs) == 1 and "all 2 items new" in errs[0], errs                  # poll 3 shares nothing with poll 2
    bad, out = report(db)
    hl = next(ln for ln in out.splitlines() if "headlines" in ln)
    assert "gaps 1" in hl and "(n=3)" in hl, hl                                  # b1, c1, c2; backlog a1 excluded
    assert 25 <= int(re.search(r"p50/p90 (\d+)s", hl)[1]) <= 90, hl            # IST dates read with tz 5.5
    req = next(ln for ln in out.splitlines() if "articles/h" in ln)
    assert "articles/h 3 (1h)" in req, req                                       # a1's page is backlog, not counted


def test_bad_config_and_proxy_values_are_rejected(tmp_path, monkeypatch):
    import pytest
    for cfg in ({"h": {"methd": "feed"}}, {"h": {"delay": True}}, {"h": {"method": "scrape"}}, {"h": {"per_host": 0}},
                {"h": {"allow": ["(unclosed"]}}, {"h": {"allow": [1]}}, {"h": {"tz": 20}}):
        with pytest.raises(SystemExit):
            scrape.parse_args(["--db", "x.db", *sources(tmp_path, cfg)])
    for v in ("foo", "user:pw@host:1", "socks5://h:1", "env:SCRAPE_TEST_UNSET"):
        with pytest.raises(SystemExit):
            scrape.parse_args(["--proxy", v])
    monkeypatch.setenv("SCRAPE_TEST_PROXY", "http://u:secret@10.9.9.9:3128")
    assert scrape.parse_args(["--proxy", "env:SCRAPE_TEST_PROXY"]).proxy == ["http://u:secret@10.9.9.9:3128"]
    assert "secret" not in scrape.redact("http://u:secret@10.9.9.9:3128")


def test_classification():
    L = scrape.liveness
    assert L("failed", 429, None) == "rate_limited" and L("deferred", None, "Crawl-delay 30s > --max-delay 2s") == "rate_limited"
    assert L("failed", 403, None) == "restricted" and L("blocked", 200, "challenge/anti-bot page") == "restricted"
    assert L("failed", 503, None) == "outage" and L("deferred", None, "circuit open: 5 server errors (5xx)") == "outage"
    assert L("failed", None, "ConnectError('refused')") == "network" and L("deferred", None, "robots unreachable") == "network"
    assert L("deferred", None, "paused until 2026-10-17T00:00:00+00:00 (budget: 3 requests per 24 h used)") == "budget"
    assert L("failed", 404, None) == "ok" and L("blocked", None, "robots.txt") is None
    assert L("failed", None, "internal: RuntimeError()") is None


def test_old_database_opens_and_keeps_data(tmp_path):
    base, port = srv()
    db = str(tmp_path / "old.db")
    con = scrape.sqlite3.connect(db)         # the schema before events/requests/pauses/ingested existed
    old_cols = [c for c in scrape.COLS if c != "ingested"]
    con.execute(f"CREATE TABLE pages({', '.join(c + (' TEXT PRIMARY KEY' if c == 'url' else '') for c in old_cols)})")
    con.execute("CREATE TABLE assets(url TEXT PRIMARY KEY, kind TEXT, alt TEXT, status TEXT DEFAULT 'found', path TEXT, bytes INT, ctype TEXT)")
    con.execute("CREATE TABLE asset_pages(asset TEXT, page TEXT, PRIMARY KEY(asset, page))")
    con.execute("INSERT INTO pages(url, depth, status, etag, text, title) VALUES(?,0,'done','\"p\"',?, 'old')",
                (f"{base}/p/old", "kept text " * 50))
    con.execute("INSERT INTO pages(url, depth) VALUES(?, 1)", (f"{base}/p/new",))   # discovered, never fetched
    con.commit()
    con.close()
    crawl(db, [f"{base}/p/old"])
    got = {u.replace(base, ""): (s_, h_, t_) for u, s_, h_, t_ in rows(db, "SELECT url, status, http, text FROM pages")}
    assert got["/p/old"][:2] == ("done", 304) and got["/p/old"][2].startswith("kept text")
    assert got["/p/new"][0] == "done"
    bad, out = report(db)
    assert bad == [] and "HISTORY" in out


def test_report_metrics(tmp_path):
    base, port = srv()
    key = f"127.0.0.1:{port}"
    db = str(tmp_path / "m.db")
    crawl(db, [f"{base}/p/m1", f"{base}/p/m2", f"{base}/nope"])
    crawl(db, [f"{base}/p/m1", f"{base}/p/m2"])                                  # both 304
    crawl(db, [f"{base}/deny{i}" for i in range(6)], "--per-host", "1")
    run(["--db", db], [])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bad = scrape.Crawler(scrape.parse_args(["--db", db, *sources(tmp_path, {"x.com": {"method": "none", "access": "X API only"}})])).report()
    out = buf.getvalue()
    cur, hist = out.split("HISTORY")
    assert bad == [key] and "RESTRICTED" in cur and "paused until" in cur, out
    assert "NOT COLLECTED" in cur and "X API only" in cur, out
    line = next(ln for ln in hist.splitlines() if "requests" in ln)
    assert "403:5" in line and "304:2" in line and "404:4" in line, line         # status distribution (3 robots.txt 404s)
    assert "cache-hit 20%" in line, line                                          # 2 x 304 of 10 page requests
    assert "articles/h 2 (1h)" in line and "paused 0.0 h" in line and "last ok 0 min ago" in line, line


if __name__ == "__main__":
    main()                                   # the pytest-only cases need: python -m pytest test_scrape.py


class Waf(H):
    """A publisher's rate limiter, the way real ones escalate: over 4 requests in 2 s -> 429 Retry-After 1;
    a request inside that wait is a strike; 3 strikes -> 403 for good (the IP ban a disguised bot earns)."""
    def do_GET(self):
        w, now = self.server.waf, time.monotonic()
        w["log"] = [t for t in w["log"] if now - t < 2] + [now]
        if w["strikes"] >= 3:
            return self.send(403)
        if now < w["until"]:
            w["strikes"] += 1
            w["early"].append(round(w["until"] - now, 4))   # seconds too soon
            return self.send(429, Retry_After="1")
        if len(w["log"]) > 4:
            w["until"], w["429"] = now + 1, w["429"] + 1
            return self.send(429, Retry_After="1")
        base = f"http://{self.headers['Host']}"
        if self.path == "/robots.txt":
            return self.send(200, "User-agent: *\nAllow: /\n", "text/plain")
        if self.path == "/feed.rss":
            if self.headers.get("If-None-Match") == '"f"':
                return self.send(304)
            items = "".join(f"<item><title>s{i}</title><link>{base}/n/{i}</link></item>" for i in range(12))
            return self.send(200, f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>',
                             "application/rss+xml", ETag='"f"')
        w["pages"][self.path] += 1
        return self.send(200, VARIANT(self.path))


def test_rate_limited_site_never_bans_us(tmp_path):
    base, s_ = start(Waf)
    s_.waf = {"log": [], "until": 0.0, "strikes": 0, "429": 0, "early": [], "pages": Counter()}
    db = str(tmp_path / "waf.db")
    for _ in range(3):                       # three polls back to back, no gap: harsher than news.py's 20 s
        crawl(db, [f"{base}/feed.rss"], "--per-host", "1", "--max-delay", "5")
    w = s_.waf
    assert w["429"] > 0                                                         # the limit was really hit
    assert w["strikes"] == 0, w                                                 # never asked again too early (was: 15 ms early on Windows)
    st = Counter(s for (s,) in rows(db, "SELECT status FROM pages WHERE url LIKE ?", f"{base}/n/%"))
    assert st == {"done": 12}, st                                               # and still collected everything
    assert set(w["pages"].values()) == {1}                                      # each article fetched once across polls


def test_paywalled_page_keeps_only_what_it_gives_away():
    ld = ('{"@type":"NewsArticle","description":"Free summary.","isAccessibleForFree":"False","articleBody":"%s",'
          '"hasPart":{"@type":"WebPageElement","isAccessibleForFree":false,"cssSelector":".paywall"}}' % ("x" * 6000))
    paid = "<p>" + "Paid paragraph with the numbers subscribers pay for. " * 20 + "</p>"
    page = (f'<html><head><script type="application/ld+json">{ld}</script></head><body><article><h1>T</h1>'
            f'<p>{"Free lead paragraph anyone can read. " * 12}</p>{paid}<div class="story paywall">{paid}</div></article></body></html>')   # Mint: paid text twice
    free = scrape.parse_page(page.encode(), "https://p.example/a", False)["free"]
    assert free.startswith("Free summary.") and "Free lead paragraph" in free and "Paid paragraph" not in free, free
    unmarked = page.replace('class="story paywall"', 'class="story"')      # the paid part is not where the markup says
    assert scrape.parse_page(unmarked.encode(), "https://p.example/a", False)["free"] == "Free summary."
