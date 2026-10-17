"""What kinds of data each tool actually returns, measured on the self-check fixture pages.

  python bench/coverage.py

Three URLs (article page with every data type, a canvas chart fed by XHR, a PDF). A tool "got" a
kind when its output contains that kind's marker. Raw HTML fields are excluded: holding the page
source isn't extracting from it.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scrape  # noqa: E402
import test_scrape as fx  # noqa: E402

URLS = []


def ours(*flags):
    def run():
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "c.db")
        a = scrape.parse_args(["--db", db, "--delay", "0", "--allow-private", "--out", os.path.join(tmp, "f"), *flags])
        asyncio.run(scrape.Crawler(a).run(URLS))
        con = scrape.sqlite3.connect(db)
        # score only extracted content + assets, never pages.url/final_url, so the seed URL can't match a marker (audit #59)
        content = con.execute("SELECT title, date, text, meta, tables, jsonld, xhr, data FROM pages").fetchall()
        asset = con.execute("SELECT kind, alt, url FROM assets").fetchall()
        return json.dumps([content, asset])
    return run


def trafilatura_best():
    import httpx
    import trafilatura
    return json.dumps([trafilatura.extract(httpx.get(u).content, url=u, output_format="json", with_metadata=True,
                                           include_tables=True, include_images=True, include_links=True) for u in URLS])


def newspaper4k():
    import newspaper
    out = []
    for u in URLS:
        try:
            a = newspaper.article(u)
            out.append({k: getattr(a, k, None) for k in
                        ("text", "title", "authors", "publish_date", "top_image", "images", "movies", "meta_data", "tags")})
        except Exception as e:
            out.append(repr(e))
    return json.dumps(out, default=str)


def crawl4ai_best():
    from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig

    async def go():
        cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, verbose=False, capture_network_requests=True)
        async with AsyncWebCrawler(verbose=False) as c:
            rs = await c.arun_many(URLS, config=cfg)
            return json.dumps([{"markdown": r.markdown and r.markdown.raw_markdown, "media": r.media, "links": r.links,
                                "tables": r.tables, "metadata": r.metadata, "network": r.network_requests,
                                "extracted": r.extracted_content} for r in rs], default=str)
    return asyncio.run(go())


def main():
    base = fx.start_server()
    URLS.extend([f"{base}/a", f"{base}/chart", f"{base}/doc.pdf"])
    tools = {"ours (default)": ours(), "ours --xhr --download all": ours("--xhr", "--download", "image,video,audio,pdf,file"),
             "trafilatura (all include_* on)": trafilatura_best, "newspaper4k": newspaper4k,
             "Crawl4AI (browser, capture_network_requests)": crawl4ai_best}
    results = {}
    for name, fn in tools.items():
        try:
            blob = fn()
        except Exception as e:
            print(name, "failed:", repr(e)[:200], file=sys.stderr)
            blob = ""
        results[name] = {k: m in blob for k, m in fx.MARKERS.items()}

    sys.stdout.reconfigure(encoding="utf-8")
    kinds = list(fx.MARKERS)
    print("| tool | " + " | ".join(kinds) + " | total |")
    print("|---" * (len(kinds) + 2) + "|")
    for name, got in results.items():
        print(f"| {name} | " + " | ".join("✅" if got[k] else "❌" for k in kinds) + f" | {sum(got.values())}/{len(kinds)} |")


if __name__ == "__main__":  # the process pool re-imports this file on Windows
    main()
