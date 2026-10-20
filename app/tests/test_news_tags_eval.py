"""How well the News page's stock tags do on real headlines, hand-labelled from the live News page (Oct 17-20 2026):
a random draw from the Indian feeds, a draw from the company feeds, and BSE filings (the scrip code names the
company). want = the stocks the story is about; also = named and fine to tag (a peer it compares, a market wrap's
movers); anything else tagged is wrong. fixtures/news_tags_master.csv holds every scrip-master row those texts could
match, so the check runs anywhere.

The article-lead check (text_stocks) needs the scraped text, which is not committed: it runs where data/scrape.db and
data/scrip_master are (the server) and skips elsewhere. `python -m pytest app/tests/test_news_tags_eval.py -s` prints
the numbers."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.services import news_tags as nt
from app.services.instrument_master import DEFAULT_DATA_DIR, InstrumentMaster

FIX = Path(__file__).parent / "fixtures"
ROWS = [json.loads(line) for line in (FIX / "news_tags_eval.jsonl").open(encoding="utf-8")]
# Measured 2026-10-20, before -> after the alias/listing-name/widget work: random 0.887/0.904 -> 0.986/0.963,
# companies 0.973/0.919 -> 0.987/1.0, BSE filings 0.964/0.884 -> 1.0/1.0, article leads 0.624/0.870 -> 0.963/0.865.
FLOORS = {"random": (0.97, 0.95), "companies": (0.97, 0.98), "bse": (0.98, 0.98), "article": (0.93, 0.84)}


def _short(sym: str) -> str:
    return sym.split(":", 1)[1].rsplit("-", 1)[0]


def score(pairs) -> tuple[float, float]:
    """(precision, recall) over (row, tagged symbols): a tag is right when wanted or also; recall counts wants."""
    good = npred = found = nwant = 0
    for row, got, also in pairs:
        got = {_short(s) for s in got}
        npred += len(got)
        good += len(got & (set(row["want"]) | set(also)))
        nwant += len(row["want"])
        found += len(got & set(row["want"]))
    return round(good / max(npred, 1), 3), round(found / max(nwant, 1), 3)


@pytest.fixture(scope="module")
def fixture_master(tmp_path_factory) -> InstrumentMaster:
    d = tmp_path_factory.mktemp("master")
    with (FIX / "news_tags_master.csv").open(encoding="utf-8") as src, \
            (d / "NSE_CM.csv").open("w", encoding="utf-8", newline="") as dst:
        csv.writer(dst).writerows(["1", name, "0", "1", "0.05", "INE000A01000", "0915-1530", "", "", sym, "10", "10",
                                   "1", short, "1", "-1.0", "XX", "", ""] for sym, short, name in csv.reader(src))
    return InstrumentMaster(data_dir=d)


@pytest.mark.parametrize("name", ["random", "companies", "bse"])
def test_headline_tags_precision_and_recall(fixture_master, monkeypatch, name):
    monkeypatch.setattr(nt, "get_master", lambda: fixture_master)
    rows = [r for r in ROWS if r["set"] == name]
    p, r = score((row, nt.stocks(row["title"], row["summary"]), row["also"]) for row in rows)
    print(f"\n{name}: {len(rows)} headlines, precision {p}, recall {r}")
    assert p >= FLOORS[name][0] and r >= FLOORS[name][1], (name, p, r)


def test_article_lead_precision_and_recall(monkeypatch):
    from app.services import news_wire as nw
    db = DEFAULT_DATA_DIR.parent / "scrape.db"
    if not db.exists() or not (DEFAULT_DATA_DIR / "NSE_CM.csv").exists():
        pytest.skip("needs data/scrape.db and data/scrip_master (the server)")
    master = InstrumentMaster(data_dir=DEFAULT_DATA_DIR)
    monkeypatch.setattr(nt, "get_master", lambda: master)
    rows = [r for r in ROWS if "text_also" in r]
    texts = nw.lookup([r["url"] for r in rows], db)
    pairs = [(r, nt.text_stocks(t), r["also"] + r["text_also"]) for r in rows
             if (t := (texts.get(r["url"]) or {}).get("text")) and texts[r["url"]]["status"] in ("done", "duplicate")]
    if len(pairs) < 300:
        pytest.skip(f"only {len(pairs)} labelled articles in this scrape.db")
    p, r = score(pairs)
    print(f"\narticle leads: {len(pairs)} articles, precision {p}, recall {r}")
    assert p >= FLOORS["article"][0] and r >= FLOORS["article"][1], (p, r)
