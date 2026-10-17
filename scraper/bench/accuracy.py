"""Extraction accuracy + speed on Zyte's article-extraction-benchmark (181 pages, hand-labelled articleBody).

  git clone https://github.com/scrapinghub/article-extraction-benchmark aebench
  python bench/accuracy.py aebench
"""
import asyncio
import gzip
import importlib.util
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scrape  # noqa: E402

B = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("evaluate", B / "evaluate.py")  # the benchmark's own scorer
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)
truth = json.loads((B / "ground-truth.json").read_text("utf8"))
pages = {p.name.split(".")[0]: gzip.open(p, "rt", encoding="utf8").read() for p in sorted((B / "html").glob("*.html.gz"))}


def ours(html, url):
    doc = scrape.extract(html.encode(), url)
    return doc["text"] if doc else ""


def trafilatura_default(html, url):
    import trafilatura
    return trafilatura.extract(html) or ""


def readability(html, url):  # same as the benchmark's runner
    import html_text
    from readability import Document
    return html_text.extract_text(Document(html).summary(html_partial=True))


def newspaper4k(html, url):
    import newspaper
    a = newspaper.article(url, input_html=html, fetch_images=False)  # default downloads every image
    a.parse()
    return a.text


def goose3(html, url):
    from goose3 import Goose
    return Goose().extract(raw_html=html).cleaned_text


def justext(html, url):
    import justext as j
    return " ".join(p.text for p in j.justext(html, j.get_stoplist("English"), 50, 200, 0.1, 0.2, 0.2, 200, True)
                    if not p.is_boilerplate)


def beautifulsoup(html, url):  # "no extraction" baseline: all page text
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


async def crawl4ai_all():
    from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig, DefaultMarkdownGenerator, PruningContentFilter
    cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, verbose=False, markdown_generator=DefaultMarkdownGenerator(
        content_filter=PruningContentFilter(), options={"ignore_links": True, "ignore_images": True}))
    fit, raw = {}, {}
    async with AsyncWebCrawler(verbose=False) as c:
        for k, html in pages.items():
            r = await c.arun("raw:" + html, config=cfg)
            fit[k], raw[k] = r.markdown.fit_markdown or "", r.markdown.raw_markdown or ""
    return fit, raw


def score(name, out, secs):
    m = ev.evaluate(truth, {k: {"articleBody": v} for k, v in out.items()}, 300)
    return name, m["f1"], m["f1_std"], m["precision"], m["recall"], 1000 * secs / len(pages)


rows = []
for f in (ours, trafilatura_default, readability, newspaper4k, goose3, justext, beautifulsoup):
    t0 = time.perf_counter()
    out = {}
    for k, html in pages.items():
        try:
            out[k] = f(html, truth[k]["url"]) or ""
        except Exception:
            out[k] = ""
    rows.append(score(f.__name__, out, time.perf_counter() - t0))
    print(*rows[-1][:2], file=sys.stderr)

t0 = time.perf_counter()
fit, raw = asyncio.run(crawl4ai_all())
secs = time.perf_counter() - t0
rows += [score("crawl4ai fit_markdown", fit, secs), score("crawl4ai raw_markdown", raw, secs)]

print(f"| {'extractor':<22} | F1            | precision | recall | ms/page |\n|---|---|---|---|---|")
for name, f1, sd, p, r, ms in sorted(rows, key=lambda x: -x[1]):
    print(f"| {name:<22} | {f1:.3f} ± {sd:.3f} | {p:.3f}     | {r:.3f}  | {ms:7.1f} |")
