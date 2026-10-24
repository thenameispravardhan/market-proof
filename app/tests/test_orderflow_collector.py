"""The order-flow collector: classification, storage, the 90-day sliding
window, the old layout's migration, the universe plan and the flow API.

Frames are fed straight into the recorder (no socket); times are a fixed
IST session in October 2026 so day boundaries are exact.
"""
from __future__ import annotations

import csv
import time

import pytest

from app.algo import flow_universe as fu
from app.algo import ticks

IST = 19800
DAY0 = 20746                                    # 2026-10-21 (IST day number)
T915 = DAY0 * 86400 - IST + 33300               # 09:15:00 IST that day


def frame(ltp, vol, bid, ask, t, **kw):
    return {"ltp": ltp, "vol_traded_today": vol, "bid_price": bid, "ask_price": ask, "exch_feed_time": t, **kw}


def feed(rec, full, frames):
    for f in frames:
        rec.on_tick(full, f, recv_ts=f["exch_feed_time"] + 0.2)


# ---- classification -------------------------------------------------------------

def test_lee_ready_classification_footprint_and_minute_flow(tmp_path):
    rec = ticks.TickRecorder(tmp_path)
    feed(rec, "NSE:SBIN-EQ", [
        frame(100.0, 1000, 99.95, 100.05, T915),           # anchors the day's volume
        frame(100.05, 1300, 100.0, 100.05, T915 + 10),     # +300 at the ask: buy
        frame(100.0, 1400, 100.0, 100.05, T915 + 20),      # +100 at the bid: sell
        frame(100.1, 1500, 100.05, 100.1, T915 + 30),      # +100 at the ask: buy
    ])
    rec.flush()
    st = ticks.FlowStore(tmp_path)
    (row,) = st.flow_rows("SBIN", T915, T915 + 60)
    assert row[0] == T915 and row[1:4] == (400.0, 100.0, 0.0)          # buy, sell, unk
    assert row[4] == 3 and row[5] == 4                                   # trades, frames
    assert row[6:10] == (100.05, 100.1, 100.0, 100.1)                    # o h l c of the traded prices
    assert st.minute_flow("SBIN", T915, T915 + 60) == {T915: (400.0, 100.0)}
    fp = {p: (b, s) for _, p, b, s in st.footprint("SBIN", T915, T915 + 60)}
    assert fp == {100.05: (300.0, 0.0), 100.0: (0.0, 100.0), 100.1: (100.0, 0.0)}


def test_a_feed_gap_and_the_auction_are_unk_not_prints(tmp_path):
    """Everything traded while the socket was down arrives as one jump: it
    counts as volume but never as one aggressive print; the pre-open call
    auction has no aggressor either."""
    rec = ticks.TickRecorder(tmp_path)
    feed(rec, "NSE:SBIN-EQ", [
        frame(100.0, 0, 99.9, 100.0, T915 - 500),            # pre-open: anchor
        frame(100.0, 50000, 99.9, 100.0, T915 - 400),        # the auction's match: unk
        frame(100.1, 50100, 100.0, 100.1, T915 + 5),         # +100 at the ask: buy
    ])
    rec.mark_resync()                                         # the socket reconnected
    feed(rec, "NSE:SBIN-EQ", [
        frame(101.0, 90100, 100.9, 101.0, T915 + 40),        # +40,000 traded while down: unk
        frame(101.0, 90150, 100.9, 101.0, T915 + 50),        # +50 at the ask: buy
    ])
    rec.flush()
    st = ticks.FlowStore(tmp_path)
    rows = {r[0]: r for r in st.flow_rows("SBIN", T915 - 600, T915 + 120)}
    pre = rows[T915 - 400 - (T915 - 400) % 60]
    assert pre[1:4] == (0.0, 0.0, 50000.0)
    assert rows[T915][1:4] == (150.0, 0.0, 40000.0)
    assert {p for _, p, _, _ in st.footprint("SBIN", T915, T915 + 60)} == {100.1, 101.0}
    assert st.prints("SBIN", T915 - 600, T915 + 120) == []           # no fake big print


def test_big_prints_open_interest_and_resume_after_a_restart(tmp_path):
    rec = ticks.TickRecorder(tmp_path)
    full = "NSE:NIFTY26OCTFUT"
    rec.key_of[full] = "NIFTY-FUT"
    frames = [frame(25000.0, 0, 24999.0, 25000.0, T915, OI=1000)]
    vol = 0
    for i in range(1, 41):                                     # 40 ordinary 75-lots at the ask
        vol += 75
        frames.append(frame(25000.0, vol, 24999.5, 25000.0, T915 + i, OI=1000 + i))
    vol += 1500                                                # then a 1,500 block (20x the average)
    frames.append(frame(25001.0, vol, 25000.0, 25001.0, T915 + 50, OI=2000))
    feed(rec, full, frames)
    rec.flush()
    st = ticks.FlowStore(tmp_path)
    (row,) = st.flow_rows("NIFTY-FUT", T915, T915 + 60)
    assert row[12] == 2000.0 and row[13] == vol                # open interest + cumulative volume at the minute's end
    assert st.prints("NIFTY-FUT", T915, T915 + 60) == [(T915 + 50, 25001.0, 1500.0, 1)]
    # A restart mid-minute: the minute is reloaded and keeps adding up; what
    # traded while down is unk.
    again = ticks.TickRecorder(tmp_path)
    again.key_of[full] = "NIFTY-FUT"
    assert again.resume(T915 + 55) == 1
    feed(again, full, [frame(25001.0, vol + 300, 25000.0, 25001.0, T915 + 56),     # +300 while down: unk
                       frame(25001.0, vol + 375, 25000.0, 25001.0, T915 + 57)])    # +75 at the ask: buy
    again.flush()
    (row2,) = st.flow_rows("NIFTY-FUT", T915, T915 + 60)
    assert row2[1] == row[1] + 75 and row2[3] == 300.0


# ---- storage: compaction, the sliding window, the old layout --------------------

def test_compaction_moves_the_day_to_parquet_and_reads_are_unchanged(tmp_path):
    rec = ticks.TickRecorder(tmp_path)
    rec.raw_keys = {"SBIN"}
    feed(rec, "NSE:SBIN-EQ", [frame(100.0, 1000, 99.95, 100.05, T915),
                              frame(100.05, 1300, 100.0, 100.05, T915 + 10),
                              frame(100.0, 1400, 100.0, 100.05, T915 + 70)])
    rec.flush()
    rec.write_meta({"SBIN": {"symbol": "NSE:SBIN-EQ", "kind": "EQ"}}, DAY0)
    st = ticks.FlowStore(tmp_path)
    before = (st.flow_rows("SBIN", T915, T915 + 180), st.footprint("SBIN", T915, T915 + 180))
    assert rec.compact(DAY0)
    assert not (tmp_path / "today" / ticks.ymd(DAY0)).exists()
    day = tmp_path / "days" / ticks.ymd(DAY0)
    assert {p.name for p in day.iterdir()} == {"flow.parquet", "fp.parquet", "raw.parquet", "meta.json"}
    after = (st.flow_rows("SBIN", T915, T915 + 180), st.footprint("SBIN", T915, T915 + 180))
    assert [r[:6] for r in after[0]] == [tuple(float(x) if i else int(x) for i, x in enumerate(r[:6])) for r in before[0]]
    assert after[1] == before[1]
    assert st.collected_days() == [ticks.ymd(DAY0)]
    # a late frame of the same day (restart after the close) is merged, not lost
    late = ticks.TickRecorder(tmp_path)
    feed(late, "NSE:SBIN-EQ", [frame(100.0, 1400, 100.0, 100.05, T915 + 75),
                               frame(100.05, 1500, 100.0, 100.05, T915 + 80)])
    late.flush()
    late.sweep(now=T915 + 86400)
    assert [r[0] for r in st.flow_rows("SBIN", T915, T915 + 180)] == [T915, T915 + 60]


def test_the_window_keeps_exactly_the_newest_n_collected_days(tmp_path):
    """The 91st collected day pushes the 1st out; raw frames keep fewer days."""
    rec = ticks.TickRecorder(tmp_path)
    days = [ticks.ymd(DAY0 - 100 + i) for i in range(91)]
    for d in days:
        (tmp_path / "days" / d).mkdir(parents=True)
        (tmp_path / "days" / d / "flow.parquet").write_bytes(b"x")
        (tmp_path / "days" / d / "raw.parquet").write_bytes(b"x")
    assert rec.prune(90, 5) == 1 + 85
    left = sorted(p.name for p in (tmp_path / "days").iterdir())
    assert left == days[1:]
    assert sorted(p.parent.name for p in tmp_path.glob("days/*/raw.parquet")) == days[-5:]


def test_the_old_layout_is_migrated_and_an_unfinished_raw_day_replayed(tmp_path):
    import duckdb

    con = duckdb.connect()
    (tmp_path / "flow" / "SBIN").mkdir(parents=True)
    (tmp_path / "fp" / "SBIN").mkdir(parents=True)
    y = T915 - 86400
    con.execute(f"COPY (SELECT {y}::BIGINT AS minute, 10.0 AS buy, 4.0 AS sell, 2 AS trades, 3 AS ticks, "
                f"0.0 AS tot_buy, 0.0 AS tot_sell) TO '{(tmp_path / 'flow/SBIN/202610.parquet').as_posix()}' (FORMAT parquet)")
    con.execute(f"COPY (SELECT {y}::BIGINT AS minute, 99.5 AS price, 10.0 AS buy, 4.0 AS sell) "
                f"TO '{(tmp_path / 'fp/SBIN/202610.parquet').as_posix()}' (FORMAT parquet)")
    raw = tmp_path / "raw" / "INFY" / f"{ticks.ymd(DAY0)}.csv"         # today's, from the old recorder
    raw.parent.mkdir(parents=True)
    with raw.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["recv_ts", "exch_ts", "ltp", "vol", "dv", "side", "bid", "ask", "bid_size", "ask_size",
                    "tot_buy", "tot_sell"])
        w.writerow([T915 + 1, T915, 1500.0, 100, 0, 0, 1499.9, 1500.0, 0, 0, 0, 0])
        w.writerow([T915 + 11, T915 + 10, 1500.0, 400, 0, 0, 1499.9, 1500.0, 0, 0, 0, 0])   # +300 at the ask
    rec = ticks.TickRecorder(tmp_path)
    assert rec.migrate_legacy() == 2                                    # one raw file + one day of flow
    assert not list(tmp_path.glob("flow/*/*.parquet")) and not raw.exists()
    st = ticks.FlowStore(tmp_path)
    assert st.minute_flow("SBIN", y, y + 60) == {y: (10.0, 4.0)}
    assert st.footprint("SBIN", y, y + 60) == [(y, 99.5, 10.0, 4.0)]
    assert st.minute_flow("INFY", T915, T915 + 60) == {T915: (300.0, 0.0)}


# ---- the universe plan --------------------------------------------------------------

def _fo():
    exp1, exp2 = (DAY0 + 6) * 86400 - IST + 55800, (DAY0 + 13) * 86400 - IST + 55800
    m1, m2 = (DAY0 + 9) * 86400 - IST + 55800, (DAY0 + 37) * 86400 - IST + 55800

    def chain(name, lo, step, n, exp):
        return {exp: {r: {lo + i * step: f"NSE:{name}{exp}{lo + i * step:g}{r}" for i in range(n)} for r in ("CE", "PE")}}

    out = {"NIFTY": {"exch": "NSE", "fut": [(m1, "NSE:NIFTY26OCTFUT"), (m2, "NSE:NIFTY26NOVFUT")],
                     "opt": {**chain("NIFTY", 24000, 50, 41, exp1), **chain("NIFTY", 24000, 50, 41, exp2)}}}
    for name, px in (("RELIANCE", 1400), ("SBIN", 800), ("INFY", 1500)):
        out[name] = {"exch": "NSE", "fut": [(m1, f"NSE:{name}26OCTFUT"), (m2, f"NSE:{name}26NOVFUT")],
                     "opt": chain(name, px - 100, 10, 21, m1)}
    return out


BROAD = {"indices": list(ticks.INDEX_NAMES), "lists": ["nifty500"], "stocks": True, "stock_futures": True,
         "stock_options": True, "next_futures": "roll", "index_all": False}       # the stock universe switched on


def _cfg(**uni):
    cfg = ticks.load_config(ticks.ROOT / "nonexistent")
    cfg["universe"] = {**cfg["universe"], **BROAD, **uni}
    cfg["symbols"] = []
    return cfg


SPOT = {"NSE:NIFTY50-INDEX": 25012.0, "NSE:RELIANCE-EQ": 1401.0, "NSE:SBIN-EQ": 799.0, "NSE:INFY-EQ": 1500.0}
LISTS = {"nifty500": (["RELIANCE", "SBIN", "INFY", "TCS"], "test")}


def test_the_plan_puts_classes_in_priority_order_and_centres_strikes():
    plan = fu.build(_cfg(index_strikes=2, index_next_strikes=1, stock_strikes=1), ["NSE:ITC-EQ"], SPOT.get,
                    T915, fo=_fo(), lists=LISTS, eq=set(), budget=5000)
    cls = [plan.meta[plan.keys[s]]["cls"] for s in plan.symbols]
    order = [c for c in fu.CLASSES if c in cls]
    assert cls == sorted(cls, key=order.index)                         # grouped, in priority order
    assert plan.symbols[0] == "NSE:ITC-EQ"
    assert plan.classes["index_fut"] == 2 and plan.keys["NSE:NIFTY26NOVFUT"] == "NIFTY-FUT2"
    nifty = sorted({plan.meta[plan.keys[s]]["strike"] for s in plan.symbols
                    if plan.meta[plan.keys[s]]["cls"] == "index_opt"})
    assert nifty == [24900.0, 24950.0, 25000.0, 25050.0, 25100.0]    # ATM 25000 +/- 2
    assert plan.classes["index_opt2"] == 6                             # next expiry +/- 1
    assert plan.classes["stocks"] == 4 and plan.classes["stock_fut"] == 3
    assert plan.classes["fut_next"] == 0                               # 9 days to expiry: outside the roll window
    assert plan.classes["stock_opt"] == 3 * 2 * 3                      # 3 stocks, ATM +/- 1, CE + PE


def test_the_budget_cuts_the_lowest_priority_first_and_narrows_stock_options():
    full = fu.build(_cfg(index_strikes=2, index_next_strikes=1, stock_strikes=3), [], SPOT.get, T915,
                    fo=_fo(), lists=LISTS, eq=set(), budget=5000)
    need = len(full.symbols) - full.classes["stock_opt"]
    tight = fu.build(_cfg(index_strikes=2, index_next_strikes=1, stock_strikes=3), [], SPOT.get, T915,
                     fo=_fo(), lists=LISTS, eq=set(), budget=need + 3 * 2 * 3)
    assert tight.classes["stock_opt"] == 18                            # every stock ATM +/- 1 instead of +/- 3
    assert len(tight.symbols) == tight.budget
    assert tight.summary()["dropped"] == {}                            # nothing wanted was cut: k was narrowed
    small = fu.build(_cfg(), [], SPOT.get, T915, fo=_fo(), lists=LISTS, eq=set(), budget=5)
    assert small.symbols[:3] == ["NSE:NIFTY50-INDEX", "NSE:NIFTY26OCTFUT", "NSE:NIFTY26NOVFUT"]
    assert small.summary()["dropped"]["stocks"] == 4


def test_next_month_in_the_roll_window_and_strikes_stay_while_near_the_money():
    near_expiry = T915 + 5 * 86400                                     # 4 days before the monthly expiry
    plan = fu.build(_cfg(index_strikes=1, index_next_strikes=0, stock_strikes=0), [], SPOT.get, near_expiry,
                    fo=_fo(), lists=LISTS, eq=set(), budget=5000)
    assert plan.classes["fut_next"] == 3 and "NSE:SBIN26NOVFUT" in plan.symbols
    first = fu.build(_cfg(index_strikes=1, index_next_strikes=0, stock_strikes=0), [], SPOT.get, T915, fo=_fo(),
                     lists=LISTS, eq=set(), budget=5000)
    moved = {**SPOT, "NSE:NIFTY50-INDEX": 25110.0}                     # two strikes up
    later = fu.build(_cfg(index_strikes=1, index_next_strikes=0, stock_strikes=0), [], moved.get, T915 + 600,
                     prev=first.opt_sel, fo=_fo(), lists=LISTS, eq=set(), budget=5000)
    strikes = {later.meta[later.keys[s]]["strike"] for s in later.symbols if later.meta[later.keys[s]]["cls"] == "sticky"}
    assert strikes == {24950.0, 25000.0}                               # the earlier band, still near: kept


def test_index_lists_fall_back_to_market_cap_ranks(tmp_path, monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", boom)
    syms, src = fu.index_list("midcap150", tmp_path)
    assert src == "market-cap rank" and len(syms) == 150
    (tmp_path / "ind_nifty500list.csv").write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(f"Co {i},X,SYM{i},EQ,INE{i}\n" for i in range(60)))
    syms, src = fu.index_list("nifty500", tmp_path)
    assert src == "niftyindices" and syms[:2] == ["SYM0", "SYM1"]


# ---- the API ---------------------------------------------------------------------------

def test_flow_api_bars_footprint_prints_and_the_future_that_rolled(client, tmp_path, monkeypatch):
    rec = ticks.TickRecorder(tmp_path)
    monkeypatch.setattr(ticks, "_STORE", ticks.FlowStore(tmp_path))
    nov = "NSE:NIFTY26NOVFUT"
    rec.key_of[nov] = "NIFTY-FUT2"
    frames = [frame(25100.0, 0, 25099.0, 25100.0, T915)]
    vol = 0
    for i in range(1, 6):                       # 09:15-09:19: buys then sells
        vol += 50
        side_ask = i <= 3
        px = 25100.0 if side_ask else 25099.0
        frames.append(frame(px, vol, 25099.0, 25100.0, T915 + 60 * (i - 1) + 5))
    feed(rec, nov, frames)
    rec.flush()
    rec.write_meta({"NIFTY-FUT2": {"symbol": nov, "kind": "FUT"}}, DAY0)
    r = client.get("/api/algo/ticks/flow", params={"symbol": nov, "resolution": "5", "from": T915, "to": T915 + 600,
                                                    "footprint": "true", "prints": "true"})
    body = r.json()
    assert body["key"] == "NIFTY-FUT2" and body["recorded"]
    (bar,) = body["bars"]
    t, buy, sell, delta, trades, unk, oi, close, dlo, dhi = bar
    assert (t, buy, sell, delta, trades) == (T915, 150.0, 100.0, 50.0, 5)
    assert close == 25099.0 and dhi == 150.0 and dlo == 0.0          # +50 +100 +150 (peak) +100 +50
    assert {(p, b, s) for _, p, b, s in body["footprint"]} == {(25100.0, 150.0, 0.0), (25099.0, 0.0, 100.0)}
    assert body["prints"] == []


def test_config_api_validates_and_the_status_reports_the_collector(client, tmp_path, monkeypatch):
    monkeypatch.setattr(ticks, "ROOT", tmp_path)
    monkeypatch.setattr(ticks, "_STORE", ticks.FlowStore(tmp_path))
    monkeypatch.setattr(ticks, "load_config", lambda root=tmp_path: _orig_load(tmp_path))
    monkeypatch.setattr(ticks, "save_config", lambda patch, root=tmp_path: _orig_save(patch, tmp_path))
    monkeypatch.setattr(ticks, "strategy_symbols", lambda root=tmp_path: [])
    bad = client.put("/api/algo/ticks/config", json={"budget": 9000})
    assert bad.status_code == 422
    bad = client.put("/api/algo/ticks/config", json={"universe": {"lists": ["nifty9000"]}})
    assert bad.status_code == 422
    ok = client.put("/api/algo/ticks/config", json={"budget": 3000, "retention_days": 90,
                                                     "universe": {"stock_strikes": 3, "next_futures": "always"}})
    assert ok.status_code == 200
    cfg = ok.json()["config"]
    assert cfg["budget"] == 3000 and cfg["universe"]["stock_strikes"] == 3 and cfg["universe"]["next_futures"] == "always"
    ticks.write_json(tmp_path / "status.json", {"state": "running", "heartbeat": time.time(), "fps": 1200.0})
    ticks.write_json(tmp_path / "stats.json", {"SBIN": [10, 5, 300.0, 100.0, 0.0, T915], "INFY": [3, 1, 5.0, 5.0, 0.0, T915]})
    st = client.get("/api/algo/ticks/status").json()
    assert st["collector"]["alive"] and st["collector"]["fps"] == 1200.0
    assert [r[0] for r in st["top"]] == ["SBIN", "INFY"]
    assert [r[0] for r in client.get("/api/algo/ticks/status", params={"q": "inf"}).json()["top"]] == ["INFY"]


_orig_load, _orig_save = ticks.load_config, ticks.save_config


@pytest.fixture(autouse=True)
def _fresh_plan_caches(monkeypatch):
    from app.config import get_settings

    fu._CACHE.clear()
    monkeypatch.setattr(get_settings(), "FYERS_PLAN", "prime", raising=False)   # recording thousands needs Prime
    yield


def test_on_the_standard_plan_the_collector_leaves_the_socket_to_the_app(tmp_path, monkeypatch):
    """Standard: 100 data-socket symbols, 60 for the app's own prices — the 40
    left are under the collector's minimum, so it plans nothing and makes no
    quote calls, and says why."""
    from app.algo import flowd
    from app.config import get_settings
    from app.execution.data_budget import ws_split

    monkeypatch.setattr(get_settings(), "FYERS_PLAN", "standard", raising=False)
    assert ws_split() == (60, 40)
    ticks.save_config({"symbols": ["NSE:ITC-EQ"], "budget": 400}, tmp_path)
    c = flowd.Collector(tmp_path)
    assert c.configured() == 0 and c.active_budget == 0 and "Prime" in c.budget_note

    async def no_quotes(*a, **k):
        raise AssertionError("no quote calls on a plan with no room to record")
    monkeypatch.setattr(c, "refresh_quotes", no_quotes)
    import asyncio
    asyncio.run(c.replan(time.time()))
    assert c.symbols == []
    monkeypatch.setattr(get_settings(), "FYERS_PLAN", "prime", raising=False)
    assert ws_split() == (500, 4500) and flowd.Collector(tmp_path).configured() == 400


# ---- the collector process -------------------------------------------------------------

@pytest.mark.asyncio
async def test_collector_plans_reports_and_sheds_load(tmp_path, monkeypatch):
    from app.algo import flowd

    ticks.save_config({"symbols": ["NSE:ITC-EQ"], "budget": 400, "universe": BROAD}, tmp_path)
    monkeypatch.setattr(fu, "contracts", lambda *a, **k: _fo())
    monkeypatch.setattr(fu, "index_list", lambda name, cache_dir, fetch=True: (["RELIANCE", "SBIN", "INFY"], "test"))
    monkeypatch.setattr(flowd, "_account", lambda: None)
    c = flowd.Collector(tmp_path)
    c.rec.ltp.update({k.upper(): v for k, v in SPOT.items()})
    await c.replan(T915)
    plan = ticks.read_json(tmp_path / "plan.json")
    assert plan["used"] == len(c.symbols) > 50 and plan["classes"]["focus"] == 1
    assert c.rec.raw_keys == {"ITC"} and c.rec.key_of["NSE:NIFTY26NOVFUT"] == "NIFTY-FUT2"
    assert ticks.read_json(tmp_path / "today" / ticks.ymd(DAY0) / "meta.json")["NIFTY-FUT"]["symbol"] == "NSE:NIFTY26OCTFUT"
    c.write_status()
    st = ticks.read_json(tmp_path / "status.json")
    assert st["state"] == "running" and st["budget"] == {"configured": 400, "active": 400, "note": None}
    # three minutes pinned at 100% CPU in the session: the plan sheds a fifth
    for _ in range(3):
        c._cpu = (time.process_time() - 70.0, time.monotonic() - 61.0)
        shed = c.check_load(T915 + 600)
    assert shed and c.active_budget == int(len(c.symbols) * 0.8) and "CPU" in c.budget_note
    await c.replan(T915 + 600)
    assert len(c.symbols) <= c.active_budget


@pytest.mark.asyncio
async def test_supervisor_runs_the_process_only_while_switched_on(tmp_path):
    import asyncio
    import sys

    from app.algo import flowd

    ticks.save_config({"enabled": True}, tmp_path)
    sup = flowd.Supervisor(tmp_path, cmd=[sys.executable, "-c", "import time; time.sleep(30)"], poll_s=0.05)
    task = asyncio.create_task(sup.run())
    try:
        for _ in range(100):
            if sup.info()["running"]:
                break
            await asyncio.sleep(0.05)
        assert sup.info()["running"]
        ticks.save_config({"enabled": False}, tmp_path)
        for _ in range(200):
            if not sup.info()["running"]:
                break
            await asyncio.sleep(0.05)
        assert not sup.info()["running"]
    finally:
        task.cancel()
        await sup.stop()


@pytest.mark.asyncio
async def test_the_disk_guard_drops_old_days_early_then_pauses_and_resumes(tmp_path, monkeypatch):
    from collections import namedtuple

    from app.algo import flowd

    ticks.save_config({"symbols": ["NSE:ITC-EQ"], "budget": 400, "min_free_gb": 10, "universe": BROAD},
                      tmp_path)
    monkeypatch.setattr(fu, "contracts", lambda *a, **k: _fo())
    monkeypatch.setattr(fu, "index_list", lambda name, cache_dir, fetch=True: (["RELIANCE", "SBIN", "INFY"], "test"))
    monkeypatch.setattr(flowd, "_account", lambda: None)
    days = [ticks.ymd(DAY0 - 10 + i) for i in range(7)]
    for d in days:
        (tmp_path / "days" / d).mkdir(parents=True)
        (tmp_path / "days" / d / "flow.parquet").write_bytes(b"x")
    usage = namedtuple("usage", "total used free")
    free = {"gb": 8.0}
    monkeypatch.setattr(flowd.shutil, "disk_usage", lambda p: usage(100e9, 90e9, free["gb"] * 1e9))
    c = flowd.Collector(tmp_path)
    c.rec.ltp.update({k.upper(): v for k, v in SPOT.items()})
    await c.replan(T915)
    assert c.symbols
    # under the 10 GB floor: one oldest day a minute, never the newest five
    assert c.check_disk(T915) is False and "dropped " + days[0] in c.disk_note
    assert c.check_disk(T915 + 30) is False                  # once a minute
    c.check_disk(T915 + 60)
    assert sorted(p.name for p in (tmp_path / "days").iterdir()) == days[2:]
    # nothing left to drop and under half the floor: recording pauses, the socket lets go
    free["gb"] = 4.0
    assert c.check_disk(T915 + 120) is True and c.paused and c.disk_note.startswith("paused")
    await c.replan(T915 + 120)
    assert c.symbols == []
    c.write_status()
    assert ticks.read_json(tmp_path / "status.json")["disk"]["paused"] is True
    # space back: it resumes with the full plan
    free["gb"] = 50.0
    assert c.check_disk(T915 + 180) is True and not c.paused and c.disk_note is None
    await c.replan(T915 + 180)
    assert c.symbols and sorted(p.name for p in (tmp_path / "days").iterdir()) == days[2:]


@pytest.mark.asyncio
async def test_a_refused_symbol_count_cuts_the_budget_and_it_stays_cut(tmp_path, monkeypatch):
    from app.algo import flowd

    ticks.save_config({"symbols": ["NSE:ITC-EQ"], "budget": 400, "universe": BROAD}, tmp_path)
    monkeypatch.setattr(fu, "contracts", lambda *a, **k: _fo())
    monkeypatch.setattr(fu, "index_list", lambda name, cache_dir, fetch=True: (["RELIANCE", "SBIN", "INFY"], "test"))
    monkeypatch.setattr(flowd, "_account", lambda: None)
    c = flowd.Collector(tmp_path)
    c.rec.ltp.update({k.upper(): v for k, v in SPOT.items()})
    await c.replan(T915)
    planned = len(c.symbols)

    class Stream:
        limit_hits = 0
        limit_error = None
        _subscribed = {f"S{i}": f"S{i}" for i in range(60)}

    c.stream = Stream()
    assert c.check_limits(T915) is False
    Stream.limit_hits, Stream.limit_error = 1, "Please provide less than 5000 symbols"
    assert c.check_limits(T915) is True
    assert c.active_budget == c.cap == 60 and "refused" in c.budget_note    # what the socket really holds
    assert c.check_limits(T915 + 5) is False                                 # the same refusal counts once
    await c.replan(T915 + 5)
    assert len(c.symbols) <= 60 < planned
    # a healthy half hour regrows load-shed budgets, but never past the cap
    c._healthy_since = T915 - 4000
    c._cpu = (time.process_time(), time.monotonic() - 61.0)
    assert c.check_load(T915 + 60) is False and c.active_budget == 60


def test_the_config_api_takes_the_disk_floor(client, tmp_path, monkeypatch):
    monkeypatch.setattr(ticks, "ROOT", tmp_path)
    monkeypatch.setattr(ticks, "_STORE", ticks.FlowStore(tmp_path))
    monkeypatch.setattr(ticks, "load_config", lambda root=tmp_path: _orig_load(tmp_path))
    monkeypatch.setattr(ticks, "save_config", lambda patch, root=tmp_path: _orig_save(patch, tmp_path))
    monkeypatch.setattr(ticks, "strategy_symbols", lambda root=tmp_path: [])
    assert client.put("/api/algo/ticks/config", json={"min_free_gb": 0}).status_code == 422
    assert client.put("/api/algo/ticks/config", json={"min_free_gb": True}).status_code == 422
    ok = client.put("/api/algo/ticks/config", json={"min_free_gb": 12})
    assert ok.status_code == 200 and ok.json()["config"]["min_free_gb"] == 12.0


# ---- NIFTY / SENSEX only (the default scope) ------------------------------------------

def _fo_indices():
    """NIFTY (NSE) and SENSEX (BSE) with two weekly expiries each, BANKNIFTY and a stock."""
    out = _fo()
    exp1, exp2 = (DAY0 + 2) * 86400 - IST + 55800, (DAY0 + 9) * 86400 - IST + 55800
    m1 = (DAY0 + 9) * 86400 - IST + 55800
    out["SENSEX"] = {"exch": "BSE", "fut": [(m1, "BSE:SENSEX26OCTFUT")],
                     "opt": {e: {r: {82000 + i * 100: f"BSE:SENSEX{e}{82000 + i * 100}{r}" for i in range(31)}
                                 for r in ("CE", "PE")} for e in (exp1, exp2)}}
    out["BANKNIFTY"] = {"exch": "NSE", "fut": [(m1, "NSE:BANKNIFTY26OCTFUT")],
                        "opt": {m1: {r: {56000 + i * 100: f"NSE:BANKNIFTY{56000 + i * 100}{r}" for i in range(11)}
                                     for r in ("CE", "PE")}}}
    return out


IDX_SPOT = {**SPOT, "BSE:SENSEX-INDEX": 82_640.0, "NSE:NIFTYBANK-INDEX": 56_500.0}


def test_the_default_records_nifty_and_sensex_their_futures_and_every_option_that_fits():
    cfg = ticks.load_config(ticks.ROOT / "nonexistent")
    fo = _fo_indices()
    plan = fu.build(cfg, cfg["symbols"], IDX_SPOT.get, T915, fo=fo, lists=LISTS, eq=set(), budget=5000)
    names = {plan.meta[plan.keys[s]].get("name") for s in plan.symbols}
    assert names == {"NIFTY", "SENSEX"}                                 # no BANKNIFTY, no stock, no stock F&O
    assert {"NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX", "NSE:NIFTY26OCTFUT", "BSE:SENSEX26OCTFUT"} <= set(plan.symbols)
    assert "NSE:NIFTY26NOVFUT" not in plan.symbols                       # next month off
    n_opts = sum(len(side) for name in ("NIFTY", "SENSEX") for chain in fo[name]["opt"].values()
                 for side in chain.values())
    opts = [s for s in plan.symbols if plan.meta[plan.keys[s]]["kind"] in ("CE", "PE")]
    assert len(opts) == n_opts                                           # the budget allows every listed option
    assert plan.classes["index_all"] > 0 and plan.summary()["dropped"] == {}
    # a tight budget: the rest of the chains go nearest expiry, then nearest the money, first
    tight = fu.build(cfg, cfg["symbols"], IDX_SPOT.get, T915, fo=fo, lists=LISTS, eq=set(), budget=len(plan.symbols) - 40)
    rest = [tight.meta[tight.keys[s]] for s in tight.symbols if tight.meta[tight.keys[s]]["cls"] == "index_all"]
    # SENSEX's first expiry is all inside ATM +/- 15 already: NIFTY's first-expiry wings come first
    assert rest[0]["name"] == "NIFTY" and rest[0]["expiry"] == min(fo["NIFTY"]["opt"])
    atm = {"NIFTY": (25000, 50), "SENSEX": (82600, 100)}                 # ATM strike, strike step
    rank = [(sorted(fo[m["name"]]["opt"]).index(m["expiry"]), abs(m["strike"] - atm[m["name"]][0]) // atm[m["name"]][1])
            for m in rest]
    assert rank == sorted(rank)                                          # nearest expiry, then strikes from the money
    assert tight.summary()["dropped"] == {"index_all": 40}


def test_a_v2_config_moves_to_nifty_and_sensex_once_and_a_later_choice_stands(tmp_path):
    ticks.write_json(tmp_path / "config.json", {
        "version": 2, "symbols": list(ticks.V2_DEFAULT_SYMBOLS), "retention_days": 120,
        "universe": {**ticks.DEFAULT_UNIVERSE, "lists": ["nifty500", "midcap150"], "stocks": True,
                     "stock_futures": True, "stock_options": True, "next_futures": "roll", "index_strikes": 20}})
    cfg = ticks.load_config(tmp_path)
    assert cfg["version"] == ticks.CONFIG_VERSION and cfg["retention_days"] == 120
    assert cfg["symbols"] == ticks.DEFAULT_SYMBOLS
    uni = cfg["universe"]
    assert uni["indices"] == ["NIFTY", "SENSEX"] and uni["lists"] == [] and not uni["stocks"]
    assert not uni["stock_futures"] and not uni["stock_options"] and uni["index_all"]
    assert uni["index_strikes"] == 20                                    # not part of the scope: kept
    ticks.save_config({"universe": {"stocks": True, "lists": ["nifty500"]}}, tmp_path)
    again = ticks.load_config(tmp_path)
    assert again["universe"]["stocks"] and again["universe"]["lists"] == ["nifty500"]


def test_an_index_chart_reads_its_futures_flow_and_the_auction_joins_the_first_candle(client, tmp_path, monkeypatch):
    rec = ticks.TickRecorder(tmp_path)
    monkeypatch.setattr(ticks, "_STORE", ticks.FlowStore(tmp_path))
    fut = "NSE:NIFTY26OCTFUT"
    rec.key_of.update({fut: "NIFTY-FUT", "NSE:NIFTY50-INDEX": "IDX_NIFTY"})
    feed(rec, fut, [frame(25100.0, 0, 25099.0, 25100.0, T915 - 600),         # pre-open anchor
                    frame(25100.0, 500, 25099.0, 25100.0, T915 - 420),       # the pre-open match: unk
                    frame(25100.0, 550, 25099.0, 25100.0, T915 + 5),         # +50 at the ask
                    frame(25099.0, 570, 25099.0, 25100.0, T915 + 65)])       # +20 at the bid
    rec.on_tick("NSE:NIFTY50-INDEX", {"ltp": 25050.0, "exch_feed_time": T915 + 5}, recv_ts=T915 + 5.2)
    rec.flush()
    rec.write_meta({"NIFTY-FUT": {"symbol": fut, "kind": "FUT"}, "IDX_NIFTY": {"symbol": "NSE:NIFTY50-INDEX",
                                                                             "kind": "IDX"}}, DAY0)
    body = client.get("/api/algo/ticks/flow", params={"symbol": "NSE:NIFTY50-INDEX", "resolution": "5",
                                                       "from": T915 - 3600, "to": T915 + 600}).json()
    assert body["key"] == "NIFTY-FUT" and body["recorded"]
    (bar,) = body["bars"]                                                # the auction is in 09:15's candle
    assert bar[0] == T915 and bar[1:3] == [50.0, 20.0] and bar[5] == 500.0


def test_lee_ready_inside_the_spread_takes_the_nearer_side_and_a_crossed_quote_falls_to_the_tick_test(tmp_path):
    rec = ticks.TickRecorder(tmp_path)
    feed(rec, "NSE:SBIN-EQ", [
        frame(100.0, 1000, 99.0, 101.0, T915),             # anchor; quote 99 / 101 (mid 100)
        frame(100.5, 1100, 99.0, 101.0, T915 + 10),        # +100 above the mid: buy (a tick test would say up = buy too)
        frame(100.2, 1200, 99.0, 101.0, T915 + 20),        # +100 above the mid on a DOWN-tick: still a buy
        frame(99.8, 1300, 100.0, 100.0, T915 + 30),        # +100 below the mid: sell; leaves a locked 100 / 100 quote
        frame(99.9, 1400, 100.0, 100.0, T915 + 40),        # +100 against the locked quote: tick test, up = buy
    ])
    rec.flush()
    (row,) = ticks.FlowStore(tmp_path).flow_rows("SBIN", T915, T915 + 60)
    assert row[1:3] == (300.0, 100.0)


def test_options_flow_adds_up_an_indexs_calls_and_puts_in_quantity_and_premium(client, tmp_path, monkeypatch):
    rec = ticks.TickRecorder(tmp_path)
    monkeypatch.setattr(ticks, "_STORE", ticks.FlowStore(tmp_path))
    exp = (DAY0 + 6) * 86400 - IST + 55800
    ce, pe = "NSE:NIFTY26O2725000CE", "NSE:NIFTY26O2725000PE"
    meta = {"NIFTY26O2725000CE": {"symbol": ce, "kind": "CE", "name": "NIFTY", "expiry": exp, "strike": 25000.0},
            "NIFTY26O2725000PE": {"symbol": pe, "kind": "PE", "name": "NIFTY", "expiry": exp, "strike": 25000.0},
            "NIFTY-FUT": {"symbol": "NSE:NIFTY26OCTFUT", "kind": "FUT", "name": "NIFTY", "expiry": exp}}
    rec.key_of.update({m["symbol"]: k for k, m in meta.items()})

    def session(day_shift):
        t0 = T915 + day_shift * 86400
        feed(rec, ce, [frame(100.0, 0, 99.5, 100.0, t0), frame(100.0, 650, 99.5, 100.0, t0 + 5),       # 650 bought @100
                       frame(99.5, 975, 99.5, 100.0, t0 + 65)])                                         # 325 sold @99.5
        feed(rec, pe, [frame(80.0, 0, 80.0, 80.5, t0), frame(80.0, 1300, 80.0, 80.5, t0 + 5)])          # 1300 sold @80
        feed(rec, "NSE:NIFTY26OCTFUT", [frame(25100.0, 0, 25099.0, 25100.0, t0),
                                        frame(25100.0, 75, 25099.0, 25100.0, t0 + 5)])                  # not an option
        rec.flush()
        rec.write_meta(meta, DAY0 + day_shift)

    session(-1)
    rec.compact(DAY0 - 1)                                            # yesterday: parquet
    session(0)
    rec.flush()                                                      # today: the working CSVs (rows re-appended)
    body = client.get("/api/algo/ticks/options-flow", params={"symbol": "NSE:NIFTY50-INDEX", "resolution": "5",
                                                               "from": T915 - 86400, "to": T915 + 600}).json()
    assert body["name"] == "NIFTY" and body["recorded"]
    assert [b[0] for b in body["bars"]] == [T915 - 86400, T915]
    for b in body["bars"]:
        t, ce_b, ce_s, pe_b, pe_s, ce_bv, ce_sv, pe_bv, pe_sv, net = b
        assert (ce_b, ce_s, pe_b, pe_s) == (650.0, 325.0, 0.0, 1300.0)
        assert (ce_bv, ce_sv, pe_bv, pe_sv) == (65000.0, 32337.5, 0.0, 104000.0)
        assert net == (65000.0 - 32337.5) - (0.0 - 104000.0)          # calls bought, puts sold: bullish
    by_option = client.get("/api/algo/ticks/options-flow", params={"symbol": ce, "resolution": "D",
                                                                    "from": T915, "to": T915 + 600}).json()
    assert by_option["name"] == "NIFTY" and by_option["bars"][0][1] == 650.0
    assert client.get("/api/algo/ticks/options-flow", params={"symbol": "NIFTY", "from": T915, "to": T915 + 600,
                                                               "expiry": "soon"}).status_code == 422
