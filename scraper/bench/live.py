"""Real websites, every tool, same pages, checks fixed before any tool runs.

  python bench/live.py [--out results.json]

Sites are ones that allow crawling (robots.txt checked first, disallowed URLs dropped):
books.toscrape.com and quotes.toscrape.com (Zyte's scraping sandboxes), Wikipedia, two public PDFs.
Ground truth comes from an independent source per page: the book page's own product table,
the quotes site's static twin of each JS page, Wikipedia's REST summary API, a known PDF phrase.
Every tool runs politely (~1 request/s/host, honest user agent) in its own process, one after another.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
UA = "scrapebot-bench/0.2 (+https://github.com/thenameispravardhan; polite comparison run)"
BOOKS = ["a-light-in-the-attic_1000", "tipping-the-velvet_999", "soumission_998", "sharp-objects_997",
         "sapiens-a-brief-history-of-humankind_996", "the-requiem-red_995", "the-dirty-little-secrets-of-getting-your-dream-job_994",
         "the-coming-woman-a-novel-based-on-the-life-of-the-infamous-feminist-victoria-woodhull_993",
         "the-boys-in-the-boat-nine-americans-and-their-epic-quest-for-gold-at-the-1936-berlin-olympics_992",
         "the-black-maria_991"]
WIKI = ["Web_scraping", "Robots.txt", "Common_Crawl", "Bombay_Stock_Exchange", "National_Stock_Exchange_of_India"]
PDFS = {"https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf": "Dummy PDF file",
        "https://www.rfc-editor.org/rfc/rfc9309.pdf": "Robots Exclusion Protocol"}
TOOLS = ["ours", "ours --browser", "scrapy", "crawl4ai-http", "crawl4ai-browser", "trafilatura", "newspaper4k"]


def norm(s):
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(s.lower().split())


def shingles(s, n=4):
    t = re.findall(r"\w+", norm(s))
    return Counter(tuple(t[i:i + n]) for i in range(max(1, len(t) - n + 1)))


# ---------- ground truth (fetched once, before any tool) ----------
def build_truth():
    import httpx
    from lxml import html
    c = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=30)
    truth = {}
    for b in BOOKS:
        u = f"https://books.toscrape.com/catalogue/{b}/index.html"
        t = html.fromstring(c.get(u).content)
        rows = {th.text_content().strip(): td.text_content().strip() for th, td in zip(t.xpath("//table//th"), t.xpath("//table//td"))}
        desc = " ".join((t.xpath("//div[@id='product_description']/following-sibling::p[1]/text()") or [""])[0].split()[:8])
        truth[u] = {"kind": "book", "must": [t.xpath("string(//h1)").strip(), rows["UPC"], rows["Price (incl. tax)"], desc]}
        time.sleep(1)
    for n in range(1, 6):
        t = html.fromstring(c.get(f"https://quotes.toscrape.com/page/{n}/").content)
        quotes = [q.strip().strip("“”")[:40] for q in t.xpath("//span[@class='text']/text()")]
        truth[f"https://quotes.toscrape.com/js/page/{n}/"] = {"kind": "quotes (JS)", "must": quotes}
        time.sleep(1)
    for w in WIKI:
        s = c.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{w}").json()["extract"]
        truth[f"https://en.wikipedia.org/wiki/{w}"] = {"kind": "wikipedia", "summary": s}
        time.sleep(1)
    for u, phrase in PDFS.items():
        truth[u] = {"kind": "pdf", "must": [phrase]}
    return truth


def robots_filter(urls):
    import httpx
    from protego import Protego
    c = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=30)
    rp, keep = {}, []
    for u in urls:
        host = "{0.scheme}://{0.netloc}".format(urlsplit(u))
        if host not in rp:
            r = c.get(host + "/robots.txt")
            rp[host] = Protego.parse(r.text if r.status_code == 200 else "")
        if rp[host].can_fetch(u, "scrapebot") and rp[host].can_fetch(u, "*"):
            keep.append(u)
        else:
            print("robots.txt disallows, dropped:", u)
    return keep


def score(truth, outputs):
    by_kind = defaultdict(lambda: [0, 0])  # kind -> [passed checks, total checks]
    per_page = {}
    for u, t in truth.items():
        text = norm(outputs.get(u, ""))
        if t["kind"] == "wikipedia":
            want, got = shingles(t["summary"]), shingles(text)
            rec = sum(min(v, got[k]) for k, v in want.items()) / max(1, sum(want.values()))
            ok, total = int(rec >= 0.8), 1
            per_page[u] = round(rec, 2)
        else:
            ok = sum(norm(m) in text for m in t["must"] if m)
            total = len([m for m in t["must"] if m])
            per_page[u] = f"{ok}/{total}"
        by_kind[t["kind"]][0] += ok
        by_kind[t["kind"]][1] += total
    return by_kind, per_page


# ---------- tools (each runs in its own process, writes {url: all text it returned}) ----------
def run_ours(urls, out, browser):
    import asyncio
    sys.path.insert(0, str(HERE.parent))
    import scrape
    db = os.path.join(tempfile.mkdtemp(), "live.db")
    argv = ["--db", db, "--ua", UA] + (["--browser"] if browser else [])
    asyncio.run(scrape.Crawler(scrape.parse_args(argv)).run(urls))
    con = scrape.sqlite3.connect(db)
    res = {}
    for row in con.execute("SELECT url, title, text, tables, jsonld, status FROM pages"):
        res[row[0]] = " ".join(str(x) for x in row[1:5] if x)
    json.dump(res, open(out, "w"))


def run_scrapy(urls, out):
    import scrapy
    import trafilatura
    from scrapy.crawler import CrawlerProcess
    res = {}

    class S(scrapy.Spider):
        name = "live"
        start_urls = urls

        def parse(self, response):
            body = response.body
            if body[:5] == b"%PDF-":
                res[response.url] = ""  # trafilatura has no PDF support
            else:
                res[response.url] = trafilatura.extract(body, url=response.url, include_tables=True) or ""

    p = CrawlerProcess(settings={"USER_AGENT": UA, "ROBOTSTXT_OBEY": True, "DOWNLOAD_DELAY": 1,
                                 "CONCURRENT_REQUESTS_PER_DOMAIN": 2, "LOG_LEVEL": "ERROR", "TELNETCONSOLE_ENABLED": False})
    p.crawl(S)
    p.start()
    json.dump(res, open(out, "w"))


def run_crawl4ai(urls, out, http):
    import asyncio
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig, RateLimiter, SemaphoreDispatcher
    from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy

    async def go():
        cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, verbose=False, check_robots_txt=True)
        kw = {"crawler_strategy": AsyncHTTPCrawlerStrategy()} if http else {"config": BrowserConfig(user_agent=UA, verbose=False)}
        disp = SemaphoreDispatcher(semaphore_count=2, rate_limiter=RateLimiter(base_delay=(1.0, 1.2)))
        async with AsyncWebCrawler(verbose=False, **kw) as c:
            rs = await c.arun_many(urls, config=cfg, dispatcher=disp)
            return {r.url: (r.markdown.raw_markdown if r.success and r.markdown else "") for r in rs}

    json.dump(asyncio.run(go()), open(out, "w"))


def run_trafilatura(urls, out):
    import trafilatura
    res, last = {}, {}
    for u in urls:
        h = urlsplit(u).netloc
        time.sleep(max(0, 1 - (time.time() - last.get(h, 0))))
        last[h] = time.time()
        res[u] = trafilatura.extract(trafilatura.fetch_url(u) or "", url=u, include_tables=True) or ""
    json.dump(res, open(out, "w"))


def run_newspaper4k(urls, out):
    import newspaper
    res, last = {}, {}
    for u in urls:
        h = urlsplit(u).netloc
        time.sleep(max(0, 1 - (time.time() - last.get(h, 0))))
        last[h] = time.time()
        try:
            a = newspaper.article(u, fetch_images=False, browser_user_agent=UA)
            res[u] = f"{a.title} {a.text}"
        except Exception as e:
            res[u] = ""
    json.dump(res, open(out, "w"))


def child(name, urls_file, out):
    urls = json.load(open(urls_file))
    if name.startswith("ours"):
        run_ours(urls, out, browser="--browser" in name)
    elif name.startswith("crawl4ai"):
        run_crawl4ai(urls, out, http=name.endswith("http"))
    else:
        globals()["run_" + name](urls, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "live_results.json"))
    ap.add_argument("--only", nargs="*", default=TOOLS)
    a = ap.parse_args()
    truth = build_truth()
    urls = robots_filter(list(truth))
    truth = {u: t for u, t in truth.items() if u in urls}
    work = tempfile.mkdtemp()
    urls_file = os.path.join(work, "urls.json")
    json.dump(urls, open(urls_file, "w"))
    print(f"{len(urls)} pages after robots.txt check\n")
    results = {}
    for name in a.only:
        out = os.path.join(work, re.sub(r"\W+", "_", name) + ".json")
        t0 = time.perf_counter()
        p = subprocess.run([sys.executable, "-I", __file__, "--child", name, urls_file, out], capture_output=True, text=True)
        secs = time.perf_counter() - t0
        if not os.path.exists(out):
            print(f"{name}: FAILED {(p.stderr or p.stdout).strip().splitlines()[-1:]}")
            continue
        outputs = json.load(open(out))
        # tools may report the final (redirected) URL; map back by path
        outputs = {u: outputs.get(u) or outputs.get(u.rstrip("/")) or outputs.get(u + "/") or "" for u in urls}
        by_kind, per_page = score(truth, outputs)
        results[name] = {"secs": round(secs, 1), "by_kind": by_kind, "per_page": per_page}
        print(name, f"{secs:.0f}s", {k: f"{v[0]}/{v[1]}" for k, v in by_kind.items()}, flush=True)
    json.dump({"truth": truth, "results": results}, open(a.out, "w"), indent=1, default=str)
    kinds = ["book", "quotes (JS)", "wikipedia", "pdf"]
    print("\n| tool | " + " | ".join(kinds) + " | total | wall s |\n|---" * 1 + "|---" * (len(kinds) + 2) + "|")
    for name, r in results.items():
        cells = [f"{r['by_kind'][k][0]}/{r['by_kind'][k][1]}" if k in r["by_kind"] else "-" for k in kinds]
        tot = sum(v[0] for v in r["by_kind"].values()), sum(v[1] for v in r["by_kind"].values())
        print(f"| {name} | " + " | ".join(cells) + f" | {tot[0]}/{tot[1]} | {r['secs']} |")
    print(f"\nfull per-page results: {a.out}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        child(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        main()
