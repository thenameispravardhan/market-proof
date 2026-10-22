"""B5: point-in-time index membership, its use in backtests, and the point-in-time FILING operand."""
from __future__ import annotations

from datetime import date

import pytest

from app.algo import engine
from app.algo import indicators as ind
from app.research import universe as u
from app.tests.test_algo import DAY0, _cross_spec, bars
from app.tests.test_algo_signals import BY_PRICE, _crossing, _three


def D(s: str) -> int:
    return u.day_of(date.fromisoformat(s))


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "ROOT", tmp_path)
    return tmp_path


def test_membership_walks_back_and_forward_from_the_lists(root):
    u.record_snapshot("nifty50", ["A", "B", "C", "D"], D("2025-01-12"))
    assert u.import_changes("nifty50", "date,symbol,action\n2025-01-10,D,add\n2025-01-10,E,remove\n2025-03-01,F,include\n") == \
        {"added": 3, "total": 3}
    assert u.members_at("nifty50", D("2025-01-09")) == {"A", "B", "C", "E"}      # before: D not yet in, E not yet out
    assert u.members_at("nifty50", D("2025-01-10")) == {"A", "B", "C", "D"}      # the change takes effect that day
    assert u.members_at("nifty50", D("2025-03-05")) == {"A", "B", "C", "D", "F"}  # after the last list: changes applied
    m = u.members_between("nifty50", D("2025-01-08"), D("2025-01-11"))
    assert m["E"] == {D("2025-01-08"), D("2025-01-09")} and m["D"] == {D("2025-01-10"), D("2025-01-11")}
    assert m["A"] == set(range(D("2025-01-08"), D("2025-01-12")))
    # a later list wins over the walk forward
    u.record_snapshot("nifty50", ["A", "B", "C", "D", "F", "G"], D("2025-04-01"))
    assert "G" in u.members_at("nifty50", D("2025-03-20"))
    cov = u.coverage("nifty50")
    assert cov["snapshots"] == 2 and cov["changes"] == 3 and cov["trusted_from"] == "20250110"
    assert u.members_at("nifty100", D("2025-01-10")) is None
    with pytest.raises(ValueError, match="no recorded list"):
        u.members_between("niftybank", D("2025-01-01"), D("2025-01-02"))
    with pytest.raises(ValueError, match="unknown index"):
        u.snapshots("sensex")


def test_a_change_log_is_checked_line_by_line_and_merged(root):
    for bad, msg in [("2025-13-01,X,add", "line 1: date"), ("2025-01-01,X,swap", "line 1: action"),
                     ("2025-01-01,,add", "line 1: no symbol"), ("date,symbol,action\n2025-01-01,X", "line 1: want")]:
        with pytest.raises(ValueError, match=msg):
            u.import_changes("nifty50", bad)
    u.import_changes("nifty50", "2025-01-01,NSE:X-EQ,in")
    assert u.import_changes("nifty50", "2025-01-01,x,add\n2025-02-01,Y,out") == {"added": 1, "total": 2}   # duplicates merge
    assert u.changes("nifty50") == [(D("2025-01-01"), "X", "add"), (D("2025-02-01"), "Y", "remove")]


def test_entries_only_while_a_member_and_ranking_never_picks_an_outsider():
    levels = {"NSE:A-EQ": 106.0, "NSE:B-EQ": 110.0, "NSE:C-EQ": 108.0}
    data = _crossing(levels)
    day = (DAY0 + 19800) // 86400
    data["NSE:B-EQ"]["member_days"] = {day - 1}          # B left the index the day before
    data["NSE:A-EQ"]["member_days"] = {day}
    data["NSE:C-EQ"]["member_days"] = {day}
    plain = engine.run(_three(), data)["trades"]
    assert sorted(x["symbol"] for x in plain) == ["NSE:A-EQ", "NSE:C-EQ"]
    top1 = engine.run(_three(BY_PRICE), _crossing(levels) | {k: {**v, "member_days": data[k]["member_days"]}
                                                           for k, v in _crossing(levels).items()})["trades"]
    assert [x["symbol"] for x in top1] == ["NSE:C-EQ"]   # B ranked highest but wasn't in the index


def test_universe_is_validated():
    s = engine.normalize({**_cross_spec(), "universe": {"index": "NIFTY50"}})
    assert s["universe"] == {"index": "nifty50"}
    assert "universe" not in engine.normalize(_cross_spec())
    with pytest.raises(ValueError, match="universe index"):
        engine.normalize({**_cross_spec(), "universe": {"index": "sensex"}})
    with pytest.raises(ValueError, match="share / futures"):
        engine.normalize({**_cross_spec(), "universe": {"index": "nifty50"}, "instrument": {"type": "option"}})


def test_the_loader_brings_every_past_member_and_says_which_have_no_candles(root, monkeypatch):
    import asyncio

    from app.algo import data

    u.record_snapshot("nifty50", ["AAA", "BBB"], D("2025-01-12"))
    u.import_changes("nifty50", "2025-01-10,BBB,add\n2025-01-10,GONE,remove\n2025-01-10,DEAD,remove")
    start = D("2025-01-08") * 86400 - 19800
    d = bars([100.0] * 10, start=start + 33300)

    async def ensure(fy, a, b):
        return {"key": data.store_key(fy), "note": None}

    monkeypatch.setattr(data, "ensure", ensure)
    monkeypatch.setattr(data, "load", lambda key, tf, a, b: dict(d) if key != "DEAD" else None)
    spec = engine.normalize({**_cross_spec(), "universe": {"index": "nifty50"}})
    out, notes, _ = asyncio.run(data.assemble(spec, start, start + 4 * 86400))
    assert sorted(out) == ["NSE:AAA-EQ", "NSE:BBB-EQ", "NSE:GONE-EQ"]          # GONE left during the range: still in
    assert out["NSE:GONE-EQ"]["member_days"] == {D("2025-01-08"), D("2025-01-09")}
    assert "NSE:TEST-EQ" not in out                                               # the universe replaces the list
    text = " ".join(n["note"] for n in notes)
    assert "4 stocks were members" in text and "1 of 4 past members have no candles" in text and "DEAD" in text


# ---- FILING -----------------------------------------------------------------

def test_a_filing_counts_only_from_the_candle_it_was_public_by():
    d = bars([100.0] * 12)
    close3 = DAY0 + 4 * 300                               # bar 3 closes here
    d["filings"] = [(close3, "Q2_RESULTS", 40.0), (close3 + 1, "ORDER_WIN", -20.0), (DAY0 + 9 * 300 + 5, "DIVIDEND", None)]
    f = ind.compute(d, "FILING", ind.normalize_params("FILING", {}))
    assert f["count"][2:6] == [0.0, 1.0, 2.0, 2.0]                     # at the close: that candle; a second later: the next
    assert f["bars_since"][:6] == [None, None, None, 0.0, 0.0, 1.0] and f["sentiment"][4] == -20.0
    res = ind.compute(d, "FILING", ind.normalize_params("FILING", {"event": "results"}))
    assert res["count"][3] == 1.0 and res["count"][4] == 1.0 and res["sentiment"][11] == 40.0
    div = ind.compute(d, "FILING", ind.normalize_params("FILING", {"event": "DIVIDEND"}))
    assert div["count"][9] == 1.0 and div["sentiment"][9] == 0.0      # no AI score: 0
    later = bars([100.0] * 300)
    later["filings"] = [(DAY0 + 600, "ORDER_WIN", 55.0)]
    w = ind.compute(later, "FILING", ind.normalize_params("FILING", {"days": 1}))
    assert w["count"][1] == 1.0 and w["count"][1 + 288] == 0.0 and w["sentiment"][1 + 288] == 0.0   # out of the 24 h window
    assert w["bars_since"][1 + 288] == 288.0
    with pytest.raises(ValueError, match="unknown event"):
        ind.normalize_params("FILING", {"event": "GOSSIP"})
    with pytest.raises(ValueError, match="can't read another symbol"):
        engine.normalize(_cross_spec(entry_long={"logic": "AND", "conditions": [
            {"left": {"ind": "FILING", "field": "count", "symbol": "SBIN"}, "op": ">", "right": {"value": 0}}]}))


def test_filing_rows_come_from_the_warehouse_at_the_moment_they_went_public(monkeypatch):
    import duckdb

    from app.algo import data
    from app.services import warehouse_store as ws

    con = duckdb.connect()
    con.execute(ws.SCHEMA)
    # stored IST; disseminated a minute after it was announced; one without dissemination time
    con.execute(f"INSERT INTO {ws.TABLE} (uid, symbol, announced_at, disseminated_at, ai_event_type, ai_sentiment_score) VALUES "
                "('a', 'SBIN', TIMESTAMP '2025-01-02 10:00:00', TIMESTAMP '2025-01-02 10:01:00', 'ORDER_WIN', 60), "
                "('b', 'SBIN', TIMESTAMP '2025-01-03 11:00:00', NULL, NULL, NULL), "
                "('c', 'TCS', TIMESTAMP '2025-01-02 10:00:00', NULL, 'DIVIDEND', 10)")
    monkeypatch.setattr(ws, "connect", lambda read_only=False: con)
    start = D("2025-01-01") * 86400 - 19800
    rows = data.filing_rows("NSE:SBIN-EQ", start, start + 10 * 86400)
    ist = lambda s, hm: D(s) * 86400 - 19800 + hm  # noqa: E731
    assert rows == [(ist("2025-01-02", 10 * 3600 + 60), "ORDER_WIN", 60.0), (ist("2025-01-03", 11 * 3600), "OTHER", None)]
    assert data.filing_rows("NSE:NIFTY50-INDEX", start, start + 86400) == []
    spec = engine.normalize(_cross_spec(entry_long={"logic": "AND", "conditions": [
        {"left": {"ind": "FILING", "params": {"event": "ORDER_WIN"}, "field": "count"}, "op": ">", "right": {"value": 0}}]}))
    assert engine.uses_filings(spec) and not engine.uses_filings(_cross_spec())


# ---- API -----------------------------------------------------------------------

def test_api_universe_routes(client, root):
    r = client.post("/api/algo/research/universe/nifty50/snapshot", json={"symbols": "AAA, NSE:BBB-EQ CCC", "date": "2025-01-12"})
    assert r.status_code == 200 and r.json()["members"] == 3 and r.json()["day"] == "20250112"
    r = client.post("/api/algo/research/universe/nifty50/changes", json={"csv": "2025-01-10,CCC,add\n2025-01-10,OLD,remove"})
    assert r.status_code == 200 and r.json()["added"] == 2 and r.json()["trusted_from"] == "20250110"
    assert client.post("/api/algo/research/universe/nifty50/changes", json={"csv": "2025-01-10,CCC"}).status_code == 422
    assert client.get("/api/algo/research/universe/nifty50/members", params={"on": "2025-01-09"}).json()["members"] == \
        ["AAA", "BBB", "OLD"]
    assert client.get("/api/algo/research/universe/sensex/members").status_code == 404
    assert client.get("/api/algo/research/universe/niftybank/members").status_code == 404
    idx = {c["index"]: c for c in client.get("/api/algo/research/universe").json()["indexes"]}
    assert idx["nifty50"]["snapshots"] == 1 and idx["niftybank"]["snapshots"] == 0
    # a backtest on a universe with no recorded list says so up front
    spec = {**_cross_spec(), "universe": {"index": "niftybank"}}
    r = client.post("/api/algo/backtest", json={"spec": spec, "start": "2025-01-01", "end": "2025-01-10"})
    assert r.status_code == 422 and "no recorded list" in r.json()["detail"]
