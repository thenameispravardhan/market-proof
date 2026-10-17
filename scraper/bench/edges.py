"""Edge-case accuracy: does each tricky page land in the right status with the right text?

  python bench/edges.py            # prints a table, exits 1 on any miss

Every case is a page shape seen on real sites (exchange/news sites included). "Silent" cases are the
dangerous ones: the failure mode is junk stored as status=done, which pollutes a corpus without any error.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scrape  # noqa: E402
import test_scrape as fx  # noqa: E402

P = fx.PARA
NAV = "<nav>" + " | ".join(f"<a href='/s{i}'>Section {i}</a>" for i in range(30)) + "</nav>"
FOOT = "<footer>" + "Copyright Example Media Ltd. All rights reserved. Terms | Privacy | Contact | Careers. " * 4 + "</footer>"


def page(title, body, head=""):
    return f"<html><head><title>{title}</title>{head}</head><body>{NAV}{body}{FOOT}</body></html>"


def art(title, marker):
    return page(title, f"<article><h1>{title}</h1><p>{marker}</p>{P * 3}</article>")


HINDI = "रिलायंस"
# path -> (code, body, ctype, headers)
R = {
    "/article": (200, art("ACME results", "MARK_ART"), "text/html", {}),
    "/branded-block": (200, page("Access Denied", "<main><h1>Access Denied</h1><p>You don't have permission to access this "
                                 "resource on this server. Reference #18.7c2d1702.1729070000.5a1b2c3</p></main>"), "text/html", {}),
    "/cf-block": (200, page("Attention Required! | Cloudflare", "<main><h1>Sorry, you have been blocked</h1><p>This website "
                            "is using a security service to protect itself from online attacks.</p></main>"), "text/html", {}),
    "/consent-wall": (200, page("Before you continue", "<main><p>We use cookies and data to deliver and maintain services. "
                                "Accept all or reject all to continue.</p></main>"), "text/html", {}),
    "/captcha-article": (200, art("How CAPTCHA tests stopped working against bots", "MARK_CAPTCHA Researchers say captcha "
                                  "puzzles and verify you are human checks no longer stop bots."), "text/html", {}),
    "/short-notice": (200, page("Board Meeting Intimation", "<article><p>MARK_SHORT The Board of Directors of ACME Ltd will "
                                "meet on 20-Oct-2026 to consider the unaudited financial results for Q2.</p></article>"), "text/html", {}),
    "/brief-404": (200, page("404 stocks hit upper circuit", "<article><p>MARK_404 As many as 404 stocks on the BSE were "
                             "locked in the upper circuit on Friday as small-caps rallied.</p></article>"), "text/html", {}),
    "/byline": (301, b"", "text/html", {"Location": "/authors/ravi-kumar"}),
    "/authors/ravi-kumar": (200, art("Ravi Kumar", "MARK_AUTHOR"), "text/html", {}),
    "/soft-404": (200, page("Page Not Found | Example News", "<main><h1>Oops! Page not found</h1><p>Sorry, the page you are "
                            "looking for does not exist or has been moved. Try searching our site.</p></main>"), "text/html", {}),
    "/attach/report.pdf": (200, page("404 - File or directory not found", "<h2>The resource you are looking for might have "
                                     "been removed, had its name changed, or is temporarily unavailable.</h2>"), "text/html", {}),
    "/members-only": (302, b"", "text/html", {"Location": "/login?next=/members-only"}),
    "/login?next=/members-only": (200, page("Sign in", "<form><label>Email</label><input name=email><label>Password</label>"
                                            "<input type=password name=pw><button>Sign in</button></form><p>Forgot password? "
                                            "Create an account to read this story and get unlimited access.</p>"), "text/html", {}),
    "/meta-refresh": (200, '<html><head><meta http-equiv="refresh" content="0; url=/moved"><title>Redirecting</title></head>'
                           '<body>Redirecting...</body></html>', "text/html", {}),
    "/moved": (200, art("Moved story", "MARK_MOVED"), "text/html", {}),
    "/story?sid=111": (200, art("Same story", "MARK_DUP"), "text/html", {}),
    "/story?sid=222": (200, art("Same story", "MARK_DUP"), "text/html", {}),
    "/cp1252": (200, art("Café results", "MARK_CP café “quoted”").replace(
        "<head>", '<head><meta charset="windows-1252">').encode("cp1252"), "text/html", {}),
    "/wrong-charset": (200, art("Reliance", "MARK_HI " + HINDI).encode("utf-8"), "text/html; charset=iso-8859-1", {}),
    "/bom-octet": (200, b"\xef\xbb\xbf<!DOCTYPE html>" + art("BOM page", "MARK_BOM").encode(), "application/octet-stream", {}),
    "/empty-200": (200, b"", "text/html", {}),
    "/rate-200": (200, "<html><head><title>Error</title></head><body>Too Many Requests</body></html>", "text/html", {}),
    "/xrobots": (200, art("Hidden", "MARK_XR"), "text/html", {"X-Robots-Tag": "noindex"}),
    "/loop1": (302, b"", "text/html", {"Location": "/loop2"}),
    "/loop2": (302, b"", "text/html", {"Location": "/loop1"}),
}
hits = {}


class H(fx.H):
    def do_GET(self):
        hits[self.path] = hits.get(self.path, 0) + 1
        if self.path == "/robots.txt":
            return self.send(200, "User-agent: *\nAllow: /\n", "text/plain")
        if self.path == "/flaky":
            return self.send(503, "busy", Retry_After="0") if hits[self.path] == 1 else self.send(200, art("Flaky", "MARK_FLAKY"))
        if self.path in R:
            code, body, ctype, hdr = R[self.path]
            return self.send(code, body, ctype, **{k.replace("-", "_"): v for k, v in hdr.items()})
        self.send(404)


class Slow(fx.H):  # declares a Crawl-delay bigger than --max-delay
    def do_GET(self):
        if self.path == "/robots.txt":
            return self.send(200, "User-agent: *\nCrawl-delay: 30\n", "text/plain")
        self.send(200, art("Slow host", "MARK_SLOW"))


def st(rows, p):
    return (rows.get(p) or {}).get("status")


def has(rows, p, marker):
    return marker in ((rows.get(p) or {}).get("text") or "")


# (case, row shown, what it guards, expected, check, silent) -- silent: the failure stores junk as done
CASES = [
    ("control article", "/article", "normal page", "done + text",
     lambda r: st(r, "/article") == "done" and has(r, "/article", "MARK_ART"), False),
    ("branded block page", "/branded-block", "Akamai-style denial with nav/footer, served 200", "blocked",
     lambda r: st(r, "/branded-block") == "blocked", True),
    ("Cloudflare block page", "/cf-block", "block page longer than the 300-char gate", "blocked",
     lambda r: st(r, "/cf-block") == "blocked", True),
    ("consent wall", "/consent-wall", "cookie interstitial instead of content", "blocked",
     lambda r: st(r, "/consent-wall") == "blocked", True),
    ("article about CAPTCHA", "/captcha-article", "block words inside a real article", "done + text",
     lambda r: st(r, "/captcha-article") == "done" and has(r, "/captcha-article", "MARK_CAPTCHA"), False),
    ("short exchange notice", "/short-notice", "legit ~150-char announcement", "done + text",
     lambda r: st(r, "/short-notice") == "done" and has(r, "/short-notice", "MARK_SHORT"), False),
    ("'404 stocks...' brief", "/brief-404", "short market brief whose title starts with 404", "done + text",
     lambda r: st(r, "/brief-404") == "done" and has(r, "/brief-404", "MARK_404"), False),
    ("redirect to /authors/", "/byline", "path that merely starts with 'auth'", "redirect, target done",
     lambda r: st(r, "/byline") == "redirect" and has(r, "/authors/ravi-kumar", "MARK_AUTHOR"), False),
    ("soft 404", "/soft-404", "'Page not found' served 200", "not done",
     lambda r: st(r, "/soft-404") not in ("done", None), True),
    ("dead PDF link -> HTML 404", "/attach/report.pdf", "attachment URL serving an IIS error page", "not done",
     lambda r: st(r, "/attach/report.pdf") not in ("done", None), True),
    ("login-wall redirect", "/members-only", "302 to /login stores no login junk", "blocked",
     lambda r: st(r, "/members-only") == "blocked" and st(r, "/login?next=/members-only") != "done", True),
    ("meta refresh", "/meta-refresh", "<meta http-equiv=refresh> hop", "redirect, target done",
     lambda r: st(r, "/meta-refresh") == "redirect" and has(r, "/moved", "MARK_MOVED"), False),
    ("session-id duplicates", "/story?sid=222", "same article under two URLs", "1 done + 1 duplicate",
     lambda r: sorted([st(r, "/story?sid=111"), st(r, "/story?sid=222")]) == ["done", "duplicate"], True),
    ("cp1252 via <meta> only", "/cp1252", "charset declared only in the page", "text decoded",
     lambda r: has(r, "/cp1252", "café “quoted”"), True),
    ("wrong header charset", "/wrong-charset", "header says latin-1, body is UTF-8 Hindi", "text decoded",
     lambda r: has(r, "/wrong-charset", HINDI), True),
    ("BOM + octet-stream", "/bom-octet", "HTML with a BOM and a wrong content type", "done + text",
     lambda r: has(r, "/bom-octet", "MARK_BOM"), False),
    ("empty 200", "/empty-200", "zero-byte body", "empty", lambda r: st(r, "/empty-200") == "empty", False),
    ("rate limit as 200", "/rate-200", "'Too Many Requests' with status 200", "blocked",
     lambda r: st(r, "/rate-200") == "blocked", True),
    ("X-Robots-Tag noindex", "/xrobots", "opt-out header", "optout, no text",
     lambda r: st(r, "/xrobots") == "optout" and not has(r, "/xrobots", "MARK_XR"), False),
    ("redirect loop", "/loop1", "r1 -> r2 -> r1", "terminates, both redirect",
     lambda r: st(r, "/loop1") == st(r, "/loop2") == "redirect", False),
    ("503 then 200", "/flaky", "transient overload with Retry-After", "done + text",
     lambda r: has(r, "/flaky", "MARK_FLAKY"), False),
    ("Crawl-delay > --max-delay", "S/x", "robots asks 30 s, cap is 2 s", "deferred",
     lambda r: st(r, "S/x") == "deferred" and st(r, "S/y") == "deferred", False),
]
SEEDS = [p for p in R if p not in ("/login?next=/members-only", "/moved", "/loop2", "/authors/ravi-kumar")] + ["/flaky"]


def run():
    base, _ = fx.start(H)
    slow, _ = fx.start(Slow)
    db = os.path.join(tempfile.mkdtemp(), "e.db")
    seeds = [base + s for s in SEEDS] + [slow + "/x", slow + "/y"]
    a = scrape.parse_args(["--db", db, "--delay", "0", "--allow-private", "--max-delay", "2", "--per-host", "4"])
    asyncio.run(scrape.Crawler(a).run(seeds))
    con = scrape.sqlite3.connect(db)
    con.row_factory = scrape.sqlite3.Row
    return {r["url"].replace(base, "").replace(slow, "S"): dict(r) for r in con.execute("SELECT * FROM pages")}


def main():
    rows = run()
    head = ("case", "guards against", "expected", "got", "result")
    out = []
    for name, key, guards, expect, check, silent in CASES:
        good = bool(check(rows))
        out.append((name, guards, expect, st(rows, key) or "-", "PASS" if good else "FAIL (silent)" if silent else "FAIL"))
    w = [max(len(r[i]) for r in out + [head]) for i in range(5)]
    for r in [head, tuple("-" * x for x in w)] + out:
        print("| " + " | ".join(c.ljust(w[i]) for i, c in enumerate(r)) + " |")
    ok = sum(r[4] == "PASS" for r in out)
    print(f"\n{ok}/{len(out)} pass")
    return ok == len(out)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
