# scraper

One file (`scrape.py`): polite async crawler -> extraction -> SQLite. Resumable.

```bash
uv venv .venv --python 3.11 && uv pip install --python .venv/Scripts/python.exe "httpx[http2]" trafilatura playwright pypdf protego
.venv/Scripts/python.exe -m playwright install chromium   # only for --browser / --xhr
.venv/Scripts/python.exe scrape.py https://site.com/sitemap.xml --db out.db --follow --download image,pdf
.venv/Scripts/python.exe scrape.py -f feeds.txt --db news.db --sources sources.example.json   # per-source limits
.venv/Scripts/python.exe scrape.py --db news.db --sources sources.example.json --report         # health; exit 1 = alert
uv pip install --python .venv/Scripts/python.exe pytest
.venv/Scripts/python.exe -m pytest test_scrape.py -q         # full suite (17 tests, ~80 s)
.venv/Scripts/python.exe test_scrape.py                    # original self-check only, prints ok
.venv/Scripts/python.exe bench/latency.py feeds.txt --minutes 375 --db latency.db --sources sources.example.json  # market hours
```

## News Wire (live headlines dashboard)

```bash
.venv/Scripts/python.exe news.py        # http://127.0.0.1:8765  (local only)
```

Every 20 s it polls `news_feeds.txt` (Moneycontrol news sitemap, NDTV, Zee Business, CNBC-TV18, ET, Mint, BusinessLine,
BSE) with `news_sources.json` limits: headlines only (title, link, time, summary), no article pages, robots.txt honoured,
refusing sources paused. Moneycontrol is polled every 40 s because its sitemap is cached for 30 s with no ETag. Data
goes to `news.db` (`items`); `news.html` shows sources, a 24 h histogram, search and the newest stories, refreshing
every 20 s. X: set `X_BEARER_TOKEN` (official API v2, pay-per-use; `X_QUERY`, daily cap `X_MAX_READS`, default 1000
posts); x.com itself disallows all bots, so it is never scraped. `python -m pytest test_news.py`.

| Data | Where it lands |
|---|---|
| Article text, title, full-timestamp date | `pages.text / title / date` |
| Author, tags, categories, summary, main image, site name; PDF author/title/date | `pages.meta` (JSON) |
| Tables (colspan expanded, caption kept, nested tables separate) | `pages.tables` |
| Structured data (schema.org JSON-LD + `__NEXT_DATA__`; lenient parse) | `pages.jsonld` |
| Same-host links with anchor text | `pages.links` |
| Chart / dashboard data loaded by JavaScript | `pages.xhr` with `--xhr`: the same-host JSON the page fetched, as numbers, not pixels |
| PDF text (+ metadata) | `pages.text` for a PDF reached as a page (seed / `--follow`); `assets.text` for a `--download`ed PDF |
| Images (src / srcset widest / `<picture>` / lazy / `og`), video, audio, PDFs, office files, iframes/embeds | `assets`; `--download kinds` saves files under `--out`, content-addressed, each fetched once |
| Your own fields | `--schema fields.json` -> LLM (OpenAI-compatible, DeepSeek default) -> `pages.data` |
| Feed headlines (RSS item / Atom entry / news-sitemap entry: title, link, published, summary) | `items`, one row per story, `first_seen` = when we first saw it |

| Crawling | How |
|---|---|
| Discovery | Sitemap / sitemap index / RSS 2.0 / Atom / news sitemap (incl. `.xml.gz` and plain-text URL lists) fan out; every hop counts toward `--depth`; feed links stay on the seeds' hosts unless `--any-host`; `--follow` walks same-host links + `rel=next` |
| Scheduler | One deque per host, round-robin: a slow host or a big `Crawl-delay` never stalls the others |
| Politeness | robots.txt (RFC 9309 via protego) + Crawl-delay (one above `--max-delay` defers the host, never crawled faster), per-host concurrency + min delay (pages, browser renders and downloads alike), host-wide backoff on 429/5xx (Retry-After); a Retry-After above `--max-delay` parks the host instead of retrying early |
| Redirects | Not followed blindly: the target is re-queued through robots + host scope; the alias row stores `final_url` |
| Re-crawl | Seeds re-polled each run unless polled less than the source's `ttl` ago (no request at all); ETag / If-Modified-Since on every re-poll that has stored text or is a feed, so an unchanged feed or article costs a 304; links already in the DB are never re-fetched by discovery; `--refresh` re-checks every row; `--retry-failed` re-queues failed/deferred/blocked rows and failed downloads |
| Junk that would pass as content | Soft 404 (by `<title>`, served 200) -> `failed`; redirect to a sign-in page -> `blocked` (`login wall`); `<meta http-equiv=refresh>` -> `redirect`; same text under another URL -> `duplicate` (`final_url` = first copy). `bench/edges.py` scores 22 such cases in a table |
| Blocks | Challenge/CAPTCHA/consent pages -> `blocked` (never rendered); empty or thin (<300 chars) pages are re-rendered with `--browser` and the longer result kept; `noindex`/`X-Robots-Tag` -> `optout` and `isAccessibleForFree:false` -> `paywalled` (no text stored; `--ignore-optout` keeps the text and flags it in `meta`) |
| Resilience | A crashed extraction pool or Chromium is rebuilt and retried; a per-request 90 s deadline; bounded exponential backoff (1, 2, 4 s, capped by `--max-delay`) for network errors and 429/5xx, retries per source (`retries`, default 3). After 5 connection failures or 5 server errors (5xx) in a row a host's remaining URLs are `deferred` for this run (circuit breaker). A failed, blocked or deferred re-poll keeps the text collected before (a content verdict - optout, paywalled, duplicate, redirect, empty - replaces it); one bad page never kills the run |
| Restrictions | 5 refusals in a row (401/403/429/CAPTCHA/challenge/login wall, page, browser render or download), a 429 on robots.txt, or a Retry-After above `--max-delay` **pause the source**: recorded in `pauses`, `ALERT` on stderr, and no request of any kind (page, robots.txt, render, download, proxy probe) until it ends, in this run or the next. Length: the Retry-After exactly, else `--pause` (or the source's `pause`, default 1 h) doubling per repeat up to 24 h. When it ends, one request probes; one more refusal pauses again. Parked URLs go back in the queue by themselves; `--resume HOST` ends a pause early once access is arranged. Never: another proxy, user agent or browser identity |
| Per-source config | `--sources sources.json` (strict: unknown keys are an error) per `host[:port]`: `method` (`headlines` = store feed items, fetch no articles; `feed` = feeds + the links they list, no link-walking, no browser; `html`; `browser`; `none` = never requested), `delay`, `per_host`, `retries`, `ttl`, `budget` (requests per rolling 24 h, counted across runs; exhausted = a `budget` pause), `pause`, `allow` (regexes discovered URLs must match), `access` (shown in `--report`), `tz` (hours, for feeds whose dates carry no zone, e.g. BSE `5.5`; applied when an item is stored). See `sources.example.json` and `SOURCES.md` |
| Network | One pooled HTTP/2 client per run (keep-alive). `--proxy URL` or `--proxy env:VAR` (keeps credentials off the command line; repeat for failover, `direct` = no proxy) is validated, then checked at start against the first seed's robots.txt; the first one that works carries the whole run (browser too). A proxy fault is no TCP connection or a 407; an origin error through a working proxy (CONNECT 502, timeout, any status) stays the origin's problem and goes to its breaker. Failover is for a dead proxy only: a 403/429 through a working proxy is the site's answer and never switches egress. Credentials are redacted from logs |
| Monitoring | `events` = every outcome, `requests` = every request sent (page, robots, render, download, probe), `pauses` = every pause; `pages.ingested` / `items.first_seen` = first sight. `--report` prints **CURRENT** per source: a live pause, else the latest source-level outcome classified `OK` / `NETWORK` / `OUTAGE` / `RATE_LIMITED` / `RESTRICTED` / `BUDGET` (a 404 page doesn't make a source down), plus `NOT COLLECTED` for `method none`, with the `access` note. **HISTORY** per source: success rate, parse failures, failures + last failure, requests sent / ok and HTTP status distribution, cache-hit rate (304s + ttl skips over page requests), duplicate rate, articles and headlines per hour, last success, hours paused, publication->ingestion p50/p90 for pages (last 24 h) and for headlines, and `gaps` (polls where every item was new, so stories may have scrolled out between polls; poll that feed more often). A feed's first-poll backlog is excluded from latency and articles/h. Exit 1 when a source isn't `OK` now (hook it to Task Scheduler / cron / an alert) |
| Crash safety | SQLite is the queue (indexed), re-run continues, discovered-but-unfetched links included (tested with a hard kill mid-crawl); downloads resume; `--forget URL|HOST` deletes rows + unreferenced files |
| Safety | SSRF guard: every request + redirect (robots.txt redirects included) + page-JS request to a loopback/private/link-local/metadata IP is refused (`--allow-private` to opt out for local testing); Chromium sandboxed, off-host navigation dropped; `--download` extension allow-list + Mark-of-the-Web; `--schema` fences page text as untrusted and keeps only schema keys |
| Caps | Browser renders skip images, media and fonts (the DOM still lists them); 4 MB HTML / 60 MB feed / `--max-file-mb` (25) for PDFs and downloads, chosen by the response's content type / `--max-files` / `--max-total-mb` / `--max-llm` |

Not extracted: what's *inside* images/audio/video (OCR, chart-image reading, speech-to-text need vision or
Whisper models), DRM/streaming media, infinite-scroll content past `--scroll` passes.
Not built, on purpose: proxy rotation, fingerprint spoofing, CAPTCHA solving, logins. A site that blocks a
polite, honest crawler is saying no: use its API, ask for access, or a licensed data provider.

## Benchmarks (2026-10-16, Windows laptop, 12 physical / 16 logical cores)

**Data coverage**: `bench/coverage.py` on the self-check fixture (one article page with every data type, a canvas
chart fed by XHR, a PDF). A tool scores a kind when the marker appears in its **named output fields** (ours:
content columns + `assets`, never the seed URL). The fixture was written alongside this scraper, so read it as
"what each tool returns on markup it handles", not a neutral benchmark — real-world variants (srcset edge cases,
scanned PDFs, >50-resource pages) are harder.

| tool | text | author | table | JSON-LD | image | video | audio | PDF link | embed | PDF text | chart (XHR) | total |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **ours `--xhr --download all`** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **11/11** |
| ours (default flags) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | 10/11 |
| Crawl4AI 0.9.5 (browser, network capture) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | 10/11 |
| newspaper4k 0.9.6 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | 6/11 |
| trafilatura 2.3 (all `include_*` on) | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | 5/11 |

Scrapy isn't here: it extracts nothing until you write per-site selectors.

**Accuracy**: `bench/accuracy.py` on [Zyte's article-extraction-benchmark](https://github.com/scrapinghub/article-extraction-benchmark)
(181 real 2019 news pages, hand-labelled article body, the benchmark's own 4-gram F1). † = output published in the
benchmark repo, scored here, not re-run (paid APIs; version/date not recorded).

| extractor | F1 | precision | recall | ms/page |
|---|---|---|---|---|
| Zyte AutoExtract † (paid) | 0.970 ± 0.005 | 0.984 | 0.956 | — |
| rs-trafilatura † | 0.970 ± 0.004 | 0.951 | 0.990 | — |
| **ours** (trafilatura `favor_precision` + metadata) | **0.964 ± 0.007** | 0.958 | 0.971 | 61 |
| go-trafilatura † | 0.960 ± 0.007 | 0.940 | 0.980 | — |
| trafilatura 2.3 default | 0.953 ± 0.006 | 0.928 | 0.978 | 34 |
| Diffbot † (paid) | 0.951 ± 0.010 | 0.958 | 0.944 | — |
| newspaper4k 0.9.6 | 0.944 ± 0.009 | 0.964 | 0.924 | 255 |
| readability-lxml 0.8.4 | 0.922 ± 0.014 | 0.913 | 0.931 | 33 |
| goose3 3.1 | 0.896 ± 0.015 | 0.940 | 0.856 | 76 |
| jusText 3.0 | 0.804 ± 0.018 | 0.858 | 0.756 | 14 |
| Crawl4AI 0.9.5 fit_markdown | 0.798 ± 0.012 | 0.678 | 0.968 | 120 |
| BeautifulSoup get_text (no extraction) | 0.665 ± 0.015 | 0.499 | 0.994 | 22 |

Honesty notes: our 0.964 is within ~1 std of the top two and ties trafilatura (ours *is* trafilatura with
`favor_precision`, chosen on this same set; held out the gain is ~+0.003). The mean hides a tail: ~72/181 pages
miss some text, 3 miss more than half, 1 is wrong — so "word for word" holds on average, not every page. Our
ms/page rose from 34 to 61 because extraction now also parses tables, JSON-LD, media and a full-timestamp date.

**Speed**: `bench/speed.py`, 905 pages (the 181 above ×5) from one local server with 100 ms/response, 32 in flight,
each crawler in its own process, **median of 3** (wall clock incl. startup). `scrapy-pool` gives Scrapy the same
multi-core extraction ours uses, so the comparison is scheduler-vs-scheduler, not our pool vs Scrapy's one core.

| crawler | pages/s (median of 3) | min–max | pages with text |
|---|---|---|---|
| Scrapy 2.19 + same process pool | 115.8 | 113.7–118.4 | 905/905 |
| **ours** (full pipeline per page) | 70.6 | 65.1–74.7 | 860/905 † |
| Scrapy 2.19 + trafilatura (one reactor core) | 41.3 | 41.1–41.8 | 905/905 |
| Crawl4AI 0.9.5, HTTP mode | 10.0 | 9.5–10.0 | 905/905 |

**Scrapy with the same multi-core extraction is faster than ours (~1.6×).** Our value isn't peak speed — it's
the full pipeline (text + metadata + tables + JSON-LD + media + politeness + resilience + opt-out handling) out of
the box, where Scrapy is a framework you build that on. Ours still beats plain single-core Scrapy ~1.7×, and the
richer per-page extraction (ms/page 34→61) is what cost us the lead over the earlier, text-only version.

† 860, not 905, because 9 of the 181 pages (×5) carry `isAccessibleForFree:false` (7) or `noindex` (2); the
crawler now honours those and stores no text — the opt-out fix (audit #44/#45), not a miss.

This is a **throughput ceiling, not a default-flags number**: every URL is one host with politeness off
(`--per-host 32 --delay 0`). At the defaults (`--per-host 2 --delay 1`) one host is capped near **1 page/s by
design** (a 100k-page site ≈ 28 h); the high rate is only reachable spread across many hosts. The ceiling is the
single asyncio I/O loop, not the network — above ~32 concurrency it gets slower. `--xhr`/`--browser` run at
browser speed (~2 pages/s), so use them only for chart/JS pages.

**Live, real websites** (`bench/live.py`, 2026-10-16): 22 pages from sites that allow crawling (books.toscrape.com,
quotes.toscrape.com/js, 5 Wikipedia articles, 2 public PDFs), robots.txt checked first, every tool polite
(~1 req/s/host) in its own process. Checks were fixed before any tool ran, from independent sources: each book
page's own product table, the quotes site's static twin pages, Wikipedia's REST summary (pass = 80% of its 4-word
phrases present), a known phrase per PDF.

| tool | books (40 checks) | JS quotes (50) | Wikipedia (5) | PDFs (2) | total | wall s |
|---|---|---|---|---|---|---|
| **ours `--browser`** | 40 | **50** | **5** | **2** | **97/97** | 13.8 |
| Crawl4AI 0.9.5 (browser) | 40 | 50 | 3 | 0 | 93/97 | 19.1 |
| ours (default) | 40 | 0 | 5 | 2 | 47/97 | 10.2 |
| Scrapy 2.19 + trafilatura | 40 | 0 | 5 | 0 | 45/97 | 14.7 |
| trafilatura 2.3 | 40 | 0 | 5 | 0 | 45/97 | 19.2 |
| Crawl4AI 0.9.5 (HTTP) | 40 | 0 | 3 | 0 | 43/97 | 12.3 |
| newspaper4k 0.9.6 | 40 | 0 | 2 | 0 | 42/97 | 24.5 |

22 pages is a small sample of easy, scraping-friendly sites; none is a news site behind bot protection.
