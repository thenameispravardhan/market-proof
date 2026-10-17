# Source reliability report (2026-10-17)

How each candidate news source can be collected by this scraper: politely, with an honest user agent
(`scrapebot/0.2 (+https://github.com/thenameispravardhan)`), and within the source's own rules.
`sources.example.json` encodes the conclusions.

**Method:** one researcher per source, at most 8 requests each, 2-3 s apart, no retries, no cookies, no other UA.
A block was recorded, not worked around. Terms were read first-hand where reachable, otherwise from search
excerpts (marked *unverified*). Probes ran 00:13-00:18 IST on Sat 17 Oct 2026, outside market hours. This is a
reading of public terms, not legal advice: confirm anything you rely on.

| Source | Honest scripted access | Terms on automated use | Config method | Reliability for the bot |
|---|---|---|---|---|
| **BSE** (bseindia.com) | `data/xml/announcements.xml` and `notices.xml`: 200, max-age 60. `.aspx` feeds: 406. `api.bseindia.com`: Akamai 403 | *Unverified*: reproduction reportedly prohibited; nothing found on automated access | `feed` (feeds + AttachLive PDFs), ttl 60, delay 5 | **Best permitted live source.** Rolling window (14-300 items) can drop items in a burst: poll every 60 s in market hours. No ETag / Last-Modified, so every poll is a full 5-20 KB. pubDate is IST with no zone marker |
| **NSE** (nseindia.com) | Only the CDN-cached robots.txt. Every page, feed and archive stalls (TCP opens, no byte for 20 s) | *Unverified*: bars "systematic or automated data collection" | `none` | Not collectable honestly. Licensed: NSE Data & Analytics corporate data feed, or an authorised vendor |
| **X** (x.com) | robots.txt `User-agent: *` / `Disallow: /`; ToS pages answer 402 to fetchers | ToS: no scraping without written consent; Developer Agreement bans training foundation models on X content | `none` | Only the X API v2 (pay-per-use; post read about $0.005). Mostly commentary that lags filings; keep out of SLM training data |
| **GDELT** (api.gdeltproject.org) | DOC API: 429 on the first request, twice, with the body "limit requests to one every 5 seconds" | Free, unrestricted use with citation | `headlines`, delay 5, ttl 900 | Permitted, but the DOC API is unreliable from this IP. 15-min batches: confirmation, never first-to-know. Raw files at `data.gdeltproject.org/gdeltv2/` are the documented bulk route |
| **The Hindu BusinessLine** | robots.txt, RSS index and feeds: 200 (Cloudflare, max-age 300) | RSS for "personal and non-commercial use"; no commercial or public use; robots blocks AI crawlers | `headlines`, ttl 300 | Clean RSS 2.0 with conditional-GET-friendly caching. Fine for your own signals; not for a competition entry or training without a licence |
| **NDTV Profit** | ndtvprofit.com: Akamai 403 on every path incl. robots.txt. `feeds.feedburner.com/ndtvprofit-latest`: 200 | Unreadable (blocked) | `headlines` on feedburner only; `none` on the site | Headlines only. Article links are blocked by design, so don't fetch them |
| **Economic Times** | Feeds, news sitemap and articles: 200, no blocks; 304s work | RSS personal non-commercial; site terms bar systematic downloading, databases and AI summarisation for real-time output | `none` | Technically easy, contractually not. Licence: Times Syndication Service |
| **Moneycontrol** | robots.txt, RSS and news sitemap: 200 | User Agreement 4.4(iii) bars bots, scrapers, crawlers | `none` | No API or licence programme found |
| **Mint** (livemint.com) | robots.txt, RSS: 200 | Terms 7A: no robots/crawlers except search engines (covers RSS) | `none` | No API found |
| **CNBC-TV18** | robots.txt, RSS: 200 | Network18 terms: no scraping or machine-readable use without written permission | `none` | Ask Network18 for written permission |
| **Financial Express** | robots.txt, syndication page, news sitemap: 200 | Personal non-commercial licence; no automated extraction or database (covers RSS) | `none` | |
| **Business Standard** | robots.txt and terms: Akamai 403 | *Unverified*: restrictive | `none` | Not reachable honestly |
| **Reuters** | robots.txt: 200; `feeds.reuters.com` no longer resolves | robots.txt: automated collection prohibited without prior written consent | `none` | Licensed: Reuters Connect API / LSEG |

## What this means

1. **The honest, permitted live feed is BSE's static RSS.** Everything else that is fast is either blocked to
   honest clients (NSE, BSE API, Business Standard, NDTV Profit) or allowed technically but barred by terms
   (ET, Moneycontrol, Mint, CNBC-TV18, FE, Reuters). Run BSE with `--sources sources.example.json` and alert
   on `--report` exit 1 or on a feed that returns zero items.
2. **To widen coverage, licence it:** NSE Data & Analytics (or a vendor such as TrueData / NSE Cogencis) for
   NSE filings, Times Syndication or Reuters Connect for news, X API v2 for social. That costs money; the
   alternative is a smaller, permitted source set.
3. **Headline-only collection** (`method: headlines`) is the least intrusive mode. It is one request per feed
   poll, a 304 when unchanged, and no article pages. Use it where a feed's terms allow your use.

## Outside the scraper: the live trading app

`app/monitors/nse.py`, `nse_rss.py` and `bse.py` send a browser User-Agent (`Mozilla/5.0 ... Chrome/124-126`).
An honest client gets nothing from NSE, so the NSE monitor most likely works only because it presents itself
as a browser. That is the kind of identity change this project's collection rules rule out, and NSE's terms
reportedly bar automated collection anyway. `bse_rss.py` (the BSE RSS monitor) sets no browser UA, and is
the monitor that matches this report. This was not changed here because it affects live trading. It needs a
decision: either an honest UA and losing NSE until it is licensed, or licensing it.
