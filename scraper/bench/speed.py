"""Crawl speed: same pages, same latency, same concurrency, every crawler in its own process.

  python bench/speed.py aebench [--copies 5] [--latency 0.1] [--conc 32] [--runs 3]

Serves the benchmark's 181 real article pages from a local server (each response delayed by
--latency to stand in for network + server time). Each runner is timed --runs times and the
median (min-max) is reported. 'scrapy-pool' gives Scrapy the SAME multi-core extraction ours
uses, so the comparison isn't our process pool vs Scrapy's single reactor core (audit #52).

NOTE: every URL is one local host with politeness off (--per-host conc --delay 0). That is a
throughput ceiling, not a default-flags run: at the defaults (--per-host 2 --delay 1) one host
is capped near 1 page/s by design. The speed is only reachable across many hosts at once.
"""
import argparse
import gzip
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNERS = ["sequential", "ours", "scrapy", "scrapy-pool", "crawl4ai-http", "crawl4ai-browser"]


def serve(pages, latency):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            time.sleep(latency)
            body = pages.get(self.path.split("/")[2] if self.path.startswith("/p/") else "", b"")
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    srv.handle_error = lambda *a: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _extract(body):
    import trafilatura
    return bool(trafilatura.extract(body))


# ---- runners: each prints "OK <pages with extracted text>" ----
def run_sequential(urls, conc):
    import httpx
    import trafilatura
    with httpx.Client() as c:
        return sum(bool(trafilatura.extract(c.get(u).content)) for u in urls)


def run_ours(urls, conc):
    import asyncio
    sys.path.insert(0, str(HERE.parent))
    import scrape
    db = os.path.join(tempfile.mkdtemp(), "s.db")
    a = scrape.parse_args(["--db", db, "--concurrency", str(conc), "--per-host", str(conc),
                           "--delay", "0", "--max-pages", "100000000", "--allow-private"])
    asyncio.run(scrape.Crawler(a).run(urls))
    return scrape.sqlite3.connect(db).execute("SELECT COUNT(*) FROM pages WHERE (status='duplicate' OR (status='done' AND text IS NOT NULL))").fetchone()[0]


def _scrapy(urls, conc, pool):
    import scrapy
    import trafilatura
    from scrapy.crawler import CrawlerProcess
    results = []

    class S(scrapy.Spider):
        name = "bench"
        start_urls = urls

        def parse(self, response):
            if pool:
                results.append(pool.submit(_extract, response.body))
            else:
                results.append(bool(trafilatura.extract(response.body)))

    p = CrawlerProcess(settings={"CONCURRENT_REQUESTS": conc, "CONCURRENT_REQUESTS_PER_DOMAIN": conc,
                                 "DOWNLOAD_DELAY": 0, "ROBOTSTXT_OBEY": False, "LOG_LEVEL": "ERROR",
                                 "TELNETCONSOLE_ENABLED": False})
    p.crawl(S)
    p.start()
    return sum(f.result() for f in results) if pool else sum(results)


def run_scrapy(urls, conc):
    return _scrapy(urls, conc, None)


def run_scrapy_pool(urls, conc):
    with ProcessPoolExecutor(min(conc, os.cpu_count() or 1)) as pool:
        return _scrapy(urls, conc, pool)


def run_crawl4ai(urls, conc, http):
    import asyncio
    from crawl4ai import (AsyncWebCrawler, CacheMode, CrawlerRunConfig, DefaultMarkdownGenerator,
                          PruningContentFilter, SemaphoreDispatcher)
    from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy

    async def go():
        cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, verbose=False, markdown_generator=DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(), options={"ignore_links": True, "ignore_images": True}))
        kw = {"crawler_strategy": AsyncHTTPCrawlerStrategy(max_connections=conc)} if http else {}
        async with AsyncWebCrawler(verbose=False, **kw) as c:
            rs = await c.arun_many(urls, config=cfg, dispatcher=SemaphoreDispatcher(semaphore_count=conc, max_session_permit=conc))
            return sum(bool(r.success and r.markdown and r.markdown.fit_markdown) for r in rs)

    return asyncio.run(go())


def child(name, urls_file, conc):
    urls = Path(urls_file).read_text().split()
    if name.startswith("crawl4ai"):
        n = run_crawl4ai(urls, conc, http=name.endswith("http"))
    else:
        n = globals()["run_" + name.replace("-", "_")](urls, conc)
    print("OK", n)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("bench")
    p.add_argument("--copies", type=int, default=5)
    p.add_argument("--latency", type=float, default=0.1)
    p.add_argument("--conc", type=int, default=32)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--only", nargs="*", default=RUNNERS)
    a = p.parse_args()
    pages = {p.name.split(".")[0]: gzip.open(p).read() for p in Path(a.bench, "html").glob("*.html.gz")}
    srv = serve(pages, a.latency)
    base = f"http://127.0.0.1:{srv.server_port}"
    urls = [f"{base}/p/{k}/{i}" for i in range(a.copies) for k in pages]
    urls_file = os.path.join(tempfile.mkdtemp(), "urls.txt")
    Path(urls_file).write_text("\n".join(urls))
    print(f"{len(urls)} pages, {a.latency * 1000:.0f} ms latency, concurrency {a.conc}, median of {a.runs}\n")
    print(f"| crawler | pages/s (median) | min-max | pages with text |\n|---|---|---|---|")
    for name in a.only:
        rates, ok = [], None
        for _ in range(a.runs):
            t0 = time.perf_counter()
            out = subprocess.run([sys.executable, "-I", __file__, "--child", name, urls_file, str(a.conc)],
                                 capture_output=True, text=True)
            secs = time.perf_counter() - t0
            got = next((ln.split()[1] for ln in out.stdout.splitlines() if ln.startswith("OK ")), None)
            if got is None:
                print(f"| {name} | failed: {(out.stderr or out.stdout).strip().splitlines()[-1:]} | | |")
                break
            rates.append(len(urls) / secs)
            ok = got
        else:
            print(f"| {name} | {statistics.median(rates):.1f} | {min(rates):.1f}-{max(rates):.1f} | {ok}/{len(urls)} |", flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        child(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        main()
