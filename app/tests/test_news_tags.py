"""News page tags: the stocks a headline names (precision first: an everyday word never tags a ticker) and what kind
of news it is; how they reach the API, the CSV export and the watchlist alerts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.services import news_tags as nt
from app.services import news_wire as nw
from app.services.event_bus import event_bus
from app.services.instrument_master import InstrumentMaster


@pytest.fixture()
def seed(tmp_path, monkeypatch):
    """The built-in seed only (no names), whatever data/scrip_master holds on this machine."""
    m = InstrumentMaster(data_dir=tmp_path / "none")
    monkeypatch.setattr(nt, "get_master", lambda: m)
    return m


def _fyers(symbol: str, name: str) -> list[str]:
    """One NSE_CM / BSE_CM row in Fyers' sym_details layout: name at 1, ticker at 9, short name at 13."""
    short = symbol.split(":", 1)[1].rsplit("-", 1)[0]
    return ["1", name, "0", "1", "0.05", "INE000A01000", "0915-1530", "", "", symbol, "10", "10", "1", short, "1",
            "-1.0", "XX", "", ""]


NAMED = [
    ("NSE:RELIANCE-EQ", "RELIANCE INDUSTRIES LTD"), ("BSE:RELIANCE-A", "RELIANCE INDUSTRIES LTD."),
    ("NSE:RPOWER-EQ", "RELIANCE POWER LTD."), ("NSE:TMPV-EQ", "TATA MOTORS PASSENGER VEHICLES LTD"),
    ("NSE:TMCV-EQ", "TATA MOTORS LIMITED"), ("NSE:SBIN-EQ", "STATE BANK OF INDIA"),
    ("NSE:CENTRALBK-EQ", "CENTRAL BANK OF INDIA"), ("NSE:OIL-EQ", "OIL INDIA LTD"),
    ("NSE:SUPREMEIND-EQ", "SUPREME INDUSTRIES LTD"), ("NSE:GOODYEAR-EQ", "GOODYEAR INDIA LTD"),
    ("NSE:NEWGEN-EQ", "NEWGEN SOFTWARE TECHNOLOGIES LTD"), ("NSE:POWERINDIA-EQ", "HITACHI ENERGY INDIA LTD"),
    ("NSE:SUNPHARMA-EQ", "SUN PHARMACEUTICAL INDUSTRIES LTD"), ("NSE:HCLTECH-EQ", "HCL TECHNOLOGIES LTD"),
    ("NSE:MARUTI-EQ", "MARUTI SUZUKI INDIA LTD."), ("NSE:DRREDDY-EQ", "DR. REDDY'S LABORATORIES LTD"),
    ("NSE:ONGC-EQ", "OIL AND NATURAL GAS CORPORATION LTD."), ("NSE:NIACL-EQ", "THE NEW INDIA ASSURANCE COMPANY LIMITED"),
    ("NSE:LICI-EQ", "LIFE INSURANCE CORPORATION OF INDIA"), ("NSE:ORIENTGREEN-EQ", "ORIENT GREEN POWER COMPANY LTD"),
    ("NSE:ICICIBANK-EQ", "ICICI BANK LTD."), ("BSE:ICICIBKX-X", "ICICI BANK LTD"),       # a big name and a tiny clash
    ("BSE:ACMEONE-X", "ACME HOLDINGS LTD"), ("BSE:ACMETWO-X", "ACME HOLDINGS LIMITED"),  # two unknowns: no one
    ("NSE:JUNKCO-EQ", "JUNKCO-EQ"),                                                    # a name that is only the ticker
    ("NSE:RAILTEL-EQ", "RailTel Corporation of India L"), ("NSE:HSCL-EQ", "HIMADRI SPECIALITY CHEM L"),   # cut at 30
    ("BSE:KAMDHENU-B", "Kamdhenu Limited"), ("NSE:858HUDCO29-N2", "8.58% HUDCO 2029"),
]


@pytest.fixture()
def named(tmp_path, monkeypatch):
    (tmp_path / "NSE_CM.csv").write_text("\n".join(",".join(_fyers(*r)) for r in NAMED) + "\n", encoding="utf-8")
    m = InstrumentMaster(data_dir=tmp_path)
    monkeypatch.setattr(nt, "get_master", lambda: m)
    return m


def _short(syms: list[str]) -> list[str]:
    return [s.split(":", 1)[1].rsplit("-", 1)[0] for s in syms]


@pytest.mark.parametrize("title,want", [
    ("Reliance Industries Q2 profit rises 10% to ₹19,323 crore", ["RELIANCE"]),
    ("RIL shares jump after Jio tariff hike", ["RELIANCE"]),                          # a media name
    ("HDFC Bank, ICICI Bank lead gains; Sensex up 500 points", ["HDFCBANK", "ICICIBANK"]),
    ("L&T bags 'mega' order worth over ₹10,000 crore", ["LT"]),
    ("Infosys, Wipro, TCS: IT stocks fall on visa curbs", ["INFY", "WIPRO", "TCS"]),
    ("Bajaj-Auto September sales up 20%", ["BAJAJ-AUTO"]),
    ("Mahindra and Mahindra launches new SUV", ["M&M"]),
    ("Dr Reddy's gets USFDA nod for generic drug", ["DRREDDY"]),
    ("Tata Steel and JSW Steel extend gains", ["TATASTEEL", "JSWSTEEL"]),
    ("Vodafone Idea shares rally 8%", ["IDEA"]),
    ("A Bright Idea For Investors", []),                                              # an everyday word
    ("Oil prices surge as OPEC cuts output", []),
    ("STOCK MARKET TODAY: TCS AND ITC IN FOCUS", []),                                 # all caps: no ticker stands out
    ("Reliance Group firms rally", []),                                               # a group, not RELIANCE
    ("Adani Group stocks under pressure", []),
    ("HDFC twins in focus", []),                                                      # HDFC alone is no listing
    ("BSE Sensex ends 300 points higher", []),                                        # the exchange as a place
    ("IPL 2027 auction: franchises spend big", []),
    ("tata steel shares", []),                                                        # lower case names no company
])
def test_stocks_a_headline_names(seed, title, want):
    assert _short(nt.stocks(title)) == want


def test_summary_only_when_the_title_names_none(seed):
    got = nt.stocks("Q2 results: profit rises 12%", "Infosys reported ... peers TCS and Wipro also")
    assert _short(got) == ["INFY", "TCS"]                                             # at most two from a summary
    assert _short(nt.stocks("Infosys Q2 profit rises", "peers TCS and Wipro also reported")) == ["INFY"]


def test_names_from_the_fyers_master(named):
    s = lambda t: _short(nt.stocks(t))   # noqa: E731
    assert s("Reliance Power shares hit upper circuit") == ["RPOWER"]                 # longest name wins
    assert s("Reliance Industries Q2") == ["RELIANCE"]
    assert s("Tata Motors Q2: CV demand strong") == ["TMCV"]                          # the master's name decides
    assert s("Tata Motors Passenger Vehicles shares fall") == ["TMPV"]
    assert s("State Bank of India raises deposit rates") == ["SBIN"] == s("State Bank Q2 results")
    assert s("Central Bank of India Q2 profit up") == ["CENTRALBK"]
    assert s("Central Bank Keeps Rates Unchanged") == []                              # "of India" cut: generic
    assert s("Oil India Q2 profit jumps") == ["OIL"] and s("Oil Prices Surge") == []
    assert s("Supreme Industries Q2") == ["SUPREMEIND"] and s("Supreme Court Stays Order") == []
    assert s("A Good Year For Banks") == [] and s("New-Gen Phones Launch") == [] and s("To Power India's Growth") == []
    assert s("Sun Pharma Q2") == ["SUNPHARMA"] == s("Sun Pharmaceutical Industries Q2")   # ticker cut along the name
    assert s("HCL Tech wins deal") == ["HCLTECH"] and s("Maruti Suzuki hikes prices") == ["MARUTI"]
    assert s("Dr. Reddy's Laboratories Q2") == ["DRREDDY"]
    assert s("Oil & Gas Stocks Rally") == [] and s("Oil and Natural Gas Corporation Q2") == ["ONGC"]
    assert s("New India Assurance Q2") == ["NIACL"] and s("Modi's New India Vision") == []
    assert s("Life Insurance Premiums Jump") == [] and s("Life Insurance Corporation Q2") == ["LICI"]
    assert s("Orient Green Power wins order") == ["ORIENTGREEN"] and s("Green Power Push By Government") == []
    assert s("ICICI Bank Q2") == ["ICICIBANK"]                                        # 10x the clash: the big one
    assert s("Acme Holdings shares jump") == []                                       # shared, sizes unknown: no one
    assert s("HDFC Bank Q2") == [] and s("Zomato Q2") == []                           # seed scrips the master dropped
    assert nt.stocks("Reliance Q2")[0] == "NSE:RELIANCE-EQ"                           # NSE listing over BSE's
    assert s("RailTel Corporation of India Ltd (543265)") == ["RAILTEL"]               # Fyers' "L" is a cut "Ltd"
    assert s("Himadri Speciality Q2 results") == ["HSCL"]                              # a cut name's start
    assert s("Kamdhenu Ltd (532741)") == ["KAMDHENU"] and s("Kamdhenu Q2") == []       # "Ltd" makes one word a company
    assert s("Forex Reserves Fall $8.58 Billion") == []                                # a bond's name starts with no word
    assert s("Buy ICICI Bank; target of Rs 1500: Reliance Power") == ["ICICIBANK"]      # the broker is not the news


def test_the_chart_button_looks_in_the_summary_and_the_article_lead_too(named):
    got = nt.find("Q2 results: profit rises 12%", "Oil India and ICICI Bank reported", "x " * 1100 + "Reliance Power")
    assert _short(got) == ["OIL", "ICICIBANK"]                                       # past the lead: peers, not the story
    assert _short(nt.find("Bank stocks fall", None, "ICICI Bank shares fell 3%")) == ["ICICIBANK"]


def test_an_everyday_word_alias_opening_a_sentence_in_prose_names_no_company(seed):
    assert nt.find("SA vs AUS 2nd Test: where to watch", None, "Toss at 9. Persistent rain is forecast.") == []
    assert nt.stocks("Q2 results", "Persistent selling hit IT stocks") == []
    assert _short(nt.find("Q2 results", "Shares of Persistent rose 4%.")) == ["PERSISTENT"]   # mid-sentence: the stock


@pytest.mark.parametrize("title,want", [
    ("Reliance Q2 net profit rises 10%; board declares dividend", ["results", "actions"]),
    ("Infosys beats estimates; revenue up 5% YoY", ["results"]),
    ("Sensex falls 500 points on profit booking", ["market"]),                       # profit booking is no result
    ("Election results: markets rally", ["market"]),
    ("L&T bags mega order worth ₹10,000 crore from Saudi", ["orders"]),
    ("KEC International secures new orders of ₹1,200 crore", ["orders"]),
    ("Tata Motors to acquire Iveco for €3.8 billion", ["deals"]),
    ("Promoter sells 2% stake via block deal", ["deals"]),
    ("Board approves 1:1 bonus issue and stock split", ["actions"]),
    ("Paytm IPO: GMP, price band and subscription status", ["ipo"]),
    ("SBI raises ₹10,000 crore via infra bonds", ["funding"]),
    ("Bond yields rise as RBI holds rates", ["macro"]),                              # bond yields are no fund-raise
    ("RBI keeps repo rate unchanged; inflation outlook cut", ["macro"]),             # an outlook, not a rating
    ("Moody's revises India outlook to positive", ["ratings"]),
    ("Motilal Oswal upgrades Titan to Buy, target ₹4,000", ["ratings"]),
    ("HUL names new CEO as Rohit Jawa steps down", ["mgmt"]),
    ("SEBI bars promoters; penalty of ₹5 crore", ["legal"]),
    ("US imposes 25% tariff on Indian goods", ["macro"]),
    ("Jio raises tariff by 15%", []),                                                # a telecom tariff is no macro
    ("Yash confirms Rs 4,000 crore budget for Ramayana", []),                        # a film's budget
    ("Budget 2027: what changes for salaried taxpayers", ["macro"]),
    ("iPhone 17 price drops: exchange bonus and more", []),                          # a shop's bonus
    ("Board recommends 1:1 bonus; record date soon", ["actions"]),
    ("Nifty hits record high; FIIs buy ₹2,000 crore", ["market"]),
])
def test_kinds(title, want):
    assert nt.kinds(title) == want


def test_watchlist_words_that_name_a_stock(seed):
    got = nt.watch_stocks(["Reliance", "tcs", "oil", "results", "Tata Steel", "HDFC"])
    assert got == {"Reliance": ["NSE:RELIANCE-EQ"], "tcs": ["NSE:TCS-EQ"], "Tata Steel": ["NSE:TATASTEEL-EQ"]}


def test_a_broken_index_never_breaks_the_page(monkeypatch):
    monkeypatch.setattr(nt, "_index", lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert nt.tag("Reliance Q2") == {"tickers": [], "tags": [], "tone": 0}


def test_headline_tone_and_article_figures():
    assert nt.tone("Bajaj Finance Q2: net profit jumps 29%, beats estimates") == 1
    assert nt.tone("Sensex falls 876 points as IT stocks slump") == -1
    assert nt.tone("Vodafone Idea Q2 loss narrows to Rs 6,000 crore") == 1      # a shrinking loss is good news
    assert nt.tone("Tata Motors shares rise; Infosys falls") == 0                # mixed
    assert nt.tone("Notice of Demand under Recovery Certificate issued against Akash Infra") == -1
    text = ("LTTS reported a net profit of ₹548 crore. Net profit jumped 54% from a year ago, while revenue rose "
            "12.5 per cent. SEBI imposed a penalty of Rs 5 lakh. Margins expanded 120 bps. It has 0% debt.")
    assert nt.figures(text) == ["a net profit of ₹548 crore", "Net profit jumped 54%", "while revenue rose 12.5 per cent",
                                "imposed a penalty of Rs 5 lakh", "Margins expanded 120 bps"]
    assert nt.figures(None) == []


_NOW = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=5))   # inside the export's 48 h on any day
RSS = f"""<?xml version="1.0"?><rss><channel>
<item><title>RIL Q2 profit rises 10%; board declares dividend</title><link>https://pub.example/r1</link>
<pubDate>{_NOW}</pubDate></item>
<item><title>Oil prices surge</title><link>https://pub.example/r2</link><pubDate>{_NOW}</pubDate></item>
</channel></rss>""".encode()
FEED = {"source": "Test", "url": "https://pub.example/rss"}


def _wire(body: bytes) -> nw.NewsWire:
    w = nw.NewsWire()
    w._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body)))
    w.state[FEED["url"]] = nw.FeedState()
    return w


def test_api_items_status_and_export_carry_the_tags(client, isolated_db, seed):
    asyncio.run(_wire(RSS).fetch(FEED, {**nw.DEFAULTS, "enabled": True}))
    items = {i["title"]: i for i in client.get("/api/news/items").json()["items"]}
    ril = items["RIL Q2 profit rises 10%; board declares dividend"]
    assert ril["tickers"] == ["NSE:RELIANCE-EQ"] and ril["tags"] == ["results", "actions"]
    assert items["Oil prices surge"]["tickers"] == [] and items["Oil prices surge"]["tags"] == ["macro"]
    s = client.get("/api/news/settings").json()
    client.put("/api/news/settings", json={**s, "watchlist": ["Reliance", "crude"]})
    st = client.get("/api/news/status").json()
    assert st["watch_stocks"] == {"Reliance": ["NSE:RELIANCE-EQ"]}
    assert ["results", "Results"] in st["kinds"]
    rows = client.get("/api/news/export.csv?hours=48").text.splitlines()
    assert rows[0].endswith(",tickers,tags") and any(r.endswith(",NSE:RELIANCE-EQ,results actions") for r in rows)
    client.put("/api/news/settings", json=s)


def test_watchlist_alert_follows_the_stock_whatever_the_headline_calls_it(isolated_db, seed):
    first = b"<rss><channel><item><title>Old story</title><link>https://pub.example/o</link></item></channel></rss>"

    async def go():
        q = event_bus.subscribe("news.alert")
        try:
            w = _wire(first)
            s = {**nw.DEFAULTS, "enabled": True, "alerts": True, "watchlist": ["Reliance"]}
            await w.fetch(FEED, s)                                   # first read: backlog, primes the feed
            w._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=RSS)))
            await w.fetch(FEED, s)
            evt = await asyncio.wait_for(q.get(), 1)
            assert evt.payload["keywords"] == ["Reliance"] and evt.payload["url"] == "https://pub.example/r1"
            assert q.empty()                                         # "Oil prices surge" names no watched stock
        finally:
            event_bus.unsubscribe("news.alert", q)

    asyncio.run(go())


def test_items_name_the_stocks_an_article_names_when_its_headline_does_not(client, isolated_db, db_session, named, tmp_path, monkeypatch):
    import importlib.util
    import sqlite3
    import sys
    import types

    from app.api import news as news_api
    from app.db.models import NewsItem

    if importlib.util.find_spec("trafilatura") is None:              # the scraper's extractor; unused on this path
        monkeypatch.setitem(sys.modules, "trafilatura", types.ModuleType("trafilatura"))
    db = tmp_path / "scrape.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE pages(url TEXT PRIMARY KEY, status TEXT, http INT, final_url TEXT, error TEXT, fetched REAL,"
                " title TEXT, date TEXT, text TEXT, meta TEXT, tables TEXT)")
    con.execute("INSERT INTO pages(url, status, text) VALUES('https://x.example/a', 'done', 'ICICI Bank shares rose 3% after the results.')")
    con.execute("INSERT INTO pages(url, status, text) VALUES('https://x.example/b', 'done', 'Oil India rose too.')")
    con.commit()
    con.close()
    monkeypatch.setattr(nw.news_wire, "scrape_db", db)
    news_api._TEXT_STOCKS.clear()
    db_session.add(NewsItem(url="https://x.example/a", source="Mint", feed="f", title="Bank stocks rally on rate-cut hopes"))
    db_session.add(NewsItem(url="https://x.example/b", source="Mint", feed="f", title="Reliance Industries hits a record"))
    db_session.commit()
    rows = {r["url"]: r for r in client.get("/api/news/items").json()["items"]}
    assert _short(rows["https://x.example/a"]["text_tickers"]) == ["ICICIBANK"]      # only the article names it
    assert rows["https://x.example/b"]["text_tickers"] == []                        # the headline already names one
    assert len(news_api._TEXT_STOCKS) == 1                                           # scanned once, then cached
