"""GET /api/search/symbols paging (`offset` / `has_more`) and the source
(`exchange`) / instrument-type (`types`) filters.

The filters run INSIDE the ranked search, before the page is cut, so
consecutive pages never overlap or skip. The tests use a small scrip
master (the in-process seed + one legacy-format CSV with F&O, BSE,
currency, MCX and ETF rows) injected in place of the process singleton.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import search as search_api
from app.services.instrument_master import (
    Instrument,
    InstrumentMaster,
    is_etf,
    search_filter,
)

# symbol, short_name, exchange, segment, instrument_type, lot, tick, expiry, strike, underlying
_ROWS = [
    ("NSE:NIFTYBEES-EQ", "NIFTYBEES", "NSE", "EQ", "EQ", "1", "0.01", "", "", ""),
    ("NSE:SETFNIF50-EQ", "SETFNIF50", "NSE", "EQ", "EQ", "1", "0.01", "", "", ""),
    ("BSE:RELIANCE-A", "RELIANCE", "BSE", "EQ", "EQ", "1", "0.05", "", "", ""),
    ("NSE:NIFTY26OCTFUT", "NIFTY", "NSE", "FO", "FUT", "75", "0.05", "27-Oct-2026", "", "NIFTY"),
    ("NSE:NIFTY26OCT25000CE", "NIFTY", "NSE", "FO", "CE", "75", "0.05", "27-Oct-2026", "25000", "NIFTY"),
    ("NSE:NIFTY26OCT25000PE", "NIFTY", "NSE", "FO", "PE", "75", "0.05", "27-Oct-2026", "25000", "NIFTY"),
    ("BSE:SENSEX26OCTFUT", "SENSEX", "BSE", "FO", "FUT", "20", "0.05", "29-Oct-2026", "", "SENSEX"),
    ("NSE:USDINR26OCTFUT", "USDINR", "NSE", "CD", "FUT", "1000", "0.0025", "28-Oct-2026", "", "USDINR"),
    ("BSE:USDINR26OCTFUT", "USDINR", "BSE", "CD", "FUT", "1000", "0.0025", "28-Oct-2026", "", "USDINR"),
    ("MCX:CRUDEOIL26OCTFUT", "CRUDEOIL", "MCX", "COM", "FUT", "100", "1", "19-Oct-2026", "", "CRUDEOIL"),
    ("NSE:FINNIFTY-INDEX", "FINNIFTY", "NSE", "INDEX", "IND", "1", "0.05", "", "", ""),
]

NFO = {"NSE:NIFTY26OCTFUT", "NSE:NIFTY26OCT25000CE", "NSE:NIFTY26OCT25000PE"}
ETFS = {"NSE:NIFTYBEES-EQ", "NSE:SETFNIF50-EQ"}
INDICES = {
    "NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "BSE:SENSEX-INDEX", "NSE:FINNIFTY-INDEX",
    "NSE:MIDCPNIFTY-INDEX", "NSE:NIFTYNXT50-INDEX", "BSE:BANKEX-INDEX",
}


@pytest.fixture()
def master(tmp_path: Path) -> InstrumentMaster:
    (tmp_path / "TEST_MIX.csv").write_text("\n".join(",".join(r) for r in _ROWS) + "\n")
    return InstrumentMaster(data_dir=tmp_path)


@pytest.fixture()
def api(client: TestClient, master: InstrumentMaster, monkeypatch) -> TestClient:
    monkeypatch.setattr(search_api, "get_master", lambda: master)
    return client


def _page(api: TestClient, **params) -> dict:
    r = api.get("/api/search/symbols", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _collect(api: TestClient, **params) -> list[str]:
    """Every hit for `params`, paging 100 at a time; asserts the pages are
    disjoint and that `offset` echoes back."""
    out: list[str] = []
    offset = 0
    while True:
        body = _page(api, limit=100, offset=offset, **params)
        assert body["offset"] == offset
        page = [h["symbol"] for h in body["hits"]]
        assert not set(page) & set(out), "pages overlap"
        out += page
        if not body["has_more"]:
            return out
        offset += 100


# ---- paging --------------------------------------------------------------


def test_pages_are_disjoint_consecutive_slices(api: TestClient) -> None:
    whole = _page(api, q="", limit=15)
    assert whole["has_more"] is True and whole["offset"] == 0
    pages = [_page(api, q="", limit=5, offset=o) for o in (0, 5, 10)]
    syms = [[h["symbol"] for h in p["hits"]] for p in pages]
    assert all(len(s) == 5 for s in syms)
    assert all(p["has_more"] is True and p["count"] == 5 for p in pages)
    assert syms[0] + syms[1] + syms[2] == [h["symbol"] for h in whole["hits"]]
    assert len(set(syms[0] + syms[1] + syms[2])) == 15


def test_has_more_turns_false_on_the_last_page(api: TestClient) -> None:
    first = _page(api, q="", exchange="NFO", limit=2)
    last = _page(api, q="", exchange="NFO", limit=2, offset=2)
    past = _page(api, q="", exchange="NFO", limit=2, offset=4)
    assert (first["count"], first["has_more"]) == (2, True)
    assert (last["count"], last["has_more"]) == (1, False)
    assert (past["count"], past["has_more"], past["hits"]) == (0, False, [])
    assert {h["symbol"] for h in first["hits"] + last["hits"]} == NFO


def test_query_paging_matches_one_big_call_across_rank_tiers(api: TestClient) -> None:
    """"NIFTY" spans the exact, prefix and substring tiers; one-hit pages
    must reassemble into exactly the single-call ranking."""
    whole = [h["symbol"] for h in _page(api, q="NIFTY", limit=100)["hits"]]
    # exact: NIFTY index + FUT/CE/PE; prefix: NIFTYBANK, NIFTYNXT50,
    # NIFTYBEES; substring: FINNIFTY, MIDCPNIFTY
    assert whole[0] == "NSE:NIFTY50-INDEX" and set(whole[1:4]) == NFO
    assert set(whole[4:7]) == {"NSE:NIFTYBANK-INDEX", "NSE:NIFTYNXT50-INDEX", "NSE:NIFTYBEES-EQ"}
    assert set(whole[7:]) == {"NSE:FINNIFTY-INDEX", "NSE:MIDCPNIFTY-INDEX"}
    paged: list[str] = []
    for offset in range(len(whole)):
        body = _page(api, q="NIFTY", limit=1, offset=offset)
        paged += [h["symbol"] for h in body["hits"]]
        assert body["has_more"] is (offset < len(whole) - 1)
    assert paged == whole


def test_offset_must_not_be_negative(api: TestClient) -> None:
    assert api.get("/api/search/symbols", params={"offset": -1}).status_code == 422


# ---- exchange ("source") filter -----------------------------------------


@pytest.mark.parametrize(
    "source, expected",
    [
        ("NFO", NFO),
        ("nfo", NFO),  # case-insensitive
        ("BFO", {"BSE:SENSEX26OCTFUT"}),
        ("CDS", {"NSE:USDINR26OCTFUT"}),
        ("BCD", {"BSE:USDINR26OCTFUT"}),
        ("MCX", {"MCX:CRUDEOIL26OCTFUT"}),
        ("BSE", {"BSE:RELIANCE-A", "BSE:SENSEX-INDEX", "BSE:BANKEX-INDEX"}),  # cash + index, no F&O / currency
        ("NFO,MCX", NFO | {"MCX:CRUDEOIL26OCTFUT"}),  # sources OR together
    ],
)
def test_exchange_filter(api: TestClient, source: str, expected: set[str]) -> None:
    assert set(_collect(api, q="", exchange=source)) == expected


def test_nse_source_is_the_cash_market_only(api: TestClient) -> None:
    hits = _page(api, q="", exchange="NSE", limit=100)["hits"]
    syms = set(_collect(api, q="", exchange="NSE"))
    assert all(h["exchange"] == "NSE" and h["segment"] not in ("FO", "CD") for h in hits)
    assert "NSE:RELIANCE-EQ" in syms and ETFS <= syms and "NSE:NIFTY50-INDEX" in syms
    assert not syms & (NFO | {"NSE:USDINR26OCTFUT"})


def test_exchange_filter_applies_to_queries(api: TestClient) -> None:
    hits = _page(api, q="RELIANCE", exchange="BSE")["hits"]
    assert [h["symbol"] for h in hits] == ["BSE:RELIANCE-A"]


def test_unknown_sources_are_ignored(api: TestClient) -> None:
    unfiltered = _collect(api, q="")
    assert _collect(api, q="", exchange="XYZ") == unfiltered
    assert set(_collect(api, q="", exchange="XYZ,NFO")) == NFO


# ---- types filter --------------------------------------------------------


@pytest.mark.parametrize(
    "types, expected",
    [
        ("FUT", {
            "NSE:NIFTY26OCTFUT", "BSE:SENSEX26OCTFUT", "NSE:USDINR26OCTFUT",
            "BSE:USDINR26OCTFUT", "MCX:CRUDEOIL26OCTFUT",
        }),
        ("OPT", {"NSE:NIFTY26OCT25000CE", "NSE:NIFTY26OCT25000PE"}),
        ("ETF", ETFS),
        ("IND", INDICES),
        ("fut,opt", {  # types OR together, case-insensitive
            "NSE:NIFTY26OCTFUT", "BSE:SENSEX26OCTFUT", "NSE:USDINR26OCTFUT",
            "BSE:USDINR26OCTFUT", "MCX:CRUDEOIL26OCTFUT",
            "NSE:NIFTY26OCT25000CE", "NSE:NIFTY26OCT25000PE",
        }),
    ],
)
def test_types_filter(api: TestClient, types: str, expected: set[str]) -> None:
    assert set(_collect(api, q="", types=types)) == expected


def test_eq_type_excludes_etfs(api: TestClient) -> None:
    syms = set(_collect(api, q="", types="EQ"))
    assert {"NSE:RELIANCE-EQ", "BSE:RELIANCE-A"} <= syms
    assert not syms & ETFS
    hits = _page(api, q="NIFTY", types="EQ")["hits"]
    assert hits == []  # NIFTYBEES is an ETF, NIFTY itself an index / F&O


def test_etf_type_matches_on_the_name_heuristic(api: TestClient) -> None:
    hits = _page(api, q="NIFTY", types="ETF")["hits"]
    assert [h["symbol"] for h in hits] == ["NSE:NIFTYBEES-EQ"]


def test_types_and_sources_combine(api: TestClient) -> None:
    assert _collect(api, q="", types="FUT", exchange="NFO") == ["NSE:NIFTY26OCTFUT"]
    assert set(_collect(api, q="", types="FUT", exchange="CDS,BCD")) == {
        "NSE:USDINR26OCTFUT", "BSE:USDINR26OCTFUT",
    }
    # the old `segment` filter still ANDs with both
    assert _collect(api, q="", types="FUT", segment="COM") == ["MCX:CRUDEOIL26OCTFUT"]


def test_response_keeps_its_shape(api: TestClient) -> None:
    body = _page(api, q="SBIN")
    assert set(body) == {"ok", "count", "offset", "has_more", "hits"}
    assert body["ok"] is True and body["count"] == len(body["hits"]) >= 1
    assert body["hits"][0]["symbol"] == "NSE:SBIN-EQ"
    assert {"symbol", "short_name", "exchange", "segment", "instrument_type", "display"} <= set(body["hits"][0])


# ---- InstrumentMaster.search(where=...) ----------------------------------


def _is_fut(i: Instrument) -> bool:
    return i.instrument_type == "FUT"


def test_where_applies_in_every_rank_tier(master: InstrumentMaster) -> None:
    # exact tier ("NIFTY" short name)
    assert [i.symbol for i in master.search("NIFTY", where=_is_fut)] == ["NSE:NIFTY26OCTFUT"]
    # prefix tier ("CRUDE" -> CRUDEOIL)
    assert [i.symbol for i in master.search("CRUDE", where=_is_fut)] == ["MCX:CRUDEOIL26OCTFUT"]
    # substring tier ("INR" inside USDINR)
    assert [i.symbol for i in master.search("INR", where=lambda i: i.exchange == "BSE")] == [
        "BSE:USDINR26OCTFUT"
    ]
    # empty-query browse
    assert [i.symbol for i in master.search("", where=lambda i: i.segment == "COM")] == [
        "MCX:CRUDEOIL26OCTFUT"
    ]


def test_where_and_segments_combine(master: InstrumentMaster) -> None:
    hits = master.search("USDINR", segments=["CD"], where=lambda i: i.exchange == "NSE")
    assert [i.symbol for i in hits] == ["NSE:USDINR26OCTFUT"]


def test_search_filter_builder() -> None:
    assert search_filter() is None
    assert search_filter(["XYZ"], ["NOPE"]) is None  # all unknown -> no filter
    nfo = search_filter(["NFO", "bogus"], None)
    assert nfo is not None
    fut = Instrument("NSE:X26OCTFUT", "X", "NSE", "FO", "FUT")
    cash = Instrument("NSE:X-EQ", "X", "NSE", "EQ", "EQ")
    assert nfo(fut) and not nfo(cash)
    both = search_filter(["NFO"], ["OPT"])
    assert both is not None and not both(fut)


def test_is_etf_heuristic() -> None:
    def eq(sym: str, short: str) -> Instrument:
        return Instrument(sym, short, "NSE", "EQ", "EQ", aliases=[short])

    assert is_etf(eq("NSE:GOLDBEES-EQ", "GOLDBEES"))
    assert is_etf(eq("NSE:CPSEETF-EQ", "CPSEETF"))
    assert is_etf(eq("NSE:setfnif50-EQ", "setfnif50"))  # case-insensitive
    assert not is_etf(eq("NSE:SBIN-EQ", "SBIN"))
    # only EQ scrips can be ETFs
    assert not is_etf(Instrument("NSE:NIFTYBEES26OCTFUT", "NIFTYBEES", "NSE", "FO", "FUT"))
