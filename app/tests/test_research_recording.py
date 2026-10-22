"""B0 research recording: option-chain snapshots, the short list kept past the
window (tiered retention) and the disk projection.

Same fixed October 2026 session as test_orderflow_collector so day
boundaries are exact; frames are fed straight into the recorder.
"""
from __future__ import annotations

import json
import time

import pytest

from app.algo import flow_universe as fu
from app.algo import ticks
from app.tests.test_orderflow_collector import DAY0, IST, LISTS, SPOT, T915, _cfg, _fo, feed, frame

T_OPEN, T_CLOSE = T915, T915 + 22500            # 09:15, 15:30 IST
CE, PE = "NSE:NIFTY26O2725000CE", "NSE:NIFTY26O2725000PE"
FUT, IDX = "NSE:NIFTY26OCTFUT", "NSE:NIFTY50-INDEX"
KEYS = {IDX: "IDX_NIFTY", FUT: "NIFTY-FUT", CE: "NIFTY26O2725000CE", PE: "NIFTY26O2725000PE"}


@pytest.fixture(autouse=True)
def _fresh_caches():
    fu._CACHE.clear()
    ticks._CAP.update(t=0.0, sig=None, v=None)
    yield


def chain_cfg(**chains):
    cfg = _cfg()
    cfg["chains"] = {**cfg["chains"], "enabled": True, **chains}
    return cfg


def chain_recorder(tmp_path, interval=60):
    rec = ticks.TickRecorder(tmp_path)
    rec.key_of.update(KEYS)
    rec.set_chain({k: "NIFTY" for k in KEYS.values()}, interval)
    return rec


def quote(ltp, bid, ask, t, vol=None, oi=None, **kw):
    msg = {"ltp": ltp, "bid_price": bid, "ask_price": ask, "bid_size": 75, "ask_size": 150, "exch_feed_time": t, **kw}
    if vol is not None:
        msg["vol_traded_today"] = vol
    if oi is not None:
        msg["OI"] = oi
    return msg


def snaps(rec):
    with rec._lock:
        return {(r[0], r[1]): r for r in rec._snaps}


# ---- config: off by default, absent = today's behaviour ---------------------------

def test_research_settings_default_off_and_merge_like_the_universe(tmp_path):
    cfg = ticks.load_config(tmp_path)
    assert cfg["chains"]["enabled"] is False and cfg["long"]["enabled"] is False
    ticks.save_config({"chains": {"enabled": True, "underlyings": [" nifty ", "NIFTY", "sbin"], "bogus": 1},
                       "long": {"days": 400}}, tmp_path)
    cfg = ticks.load_config(tmp_path)
    assert cfg["chains"]["enabled"] and cfg["chains"]["underlyings"] == ["NIFTY", "SBIN"]
    assert "bogus" not in cfg["chains"] and cfg["chains"]["strikes"] == ticks.DEFAULT_CHAINS["strikes"]
    assert cfg["long"]["days"] == 400 and cfg["long"]["enabled"] is False


def test_with_both_switches_off_the_plan_is_exactly_todays():
    args = dict(fo=_fo(), lists=LISTS, eq=set(), budget=5000)
    base = fu.build(_cfg(index_strikes=2), [IDX, "NSE:SBIN-EQ"], SPOT.get, T915, **args)
    same = fu.build(_cfg(index_strikes=2), [IDX, "NSE:SBIN-EQ"], SPOT.get, T915, strat=[IDX], **args)
    assert same.symbols == base.symbols and same.meta == base.meta
    assert not same.chain and same.classes["chain"] == 0
    assert not any(m.get(f) for m in same.meta.values() for f in ticks.META_FLAGS)


# ---- the plan --------------------------------------------------------------------------

def test_chain_name_maps_every_way_a_strategy_names_an_underlying():
    fo = _fo()
    assert fu.chain_name(IDX, fo) == "NIFTY" and fu.chain_name("NIFTY:FUT", fo) == "NIFTY"
    assert fu.chain_name("nifty", fo) == "NIFTY" and fu.chain_name("NSE:SBIN-EQ", fo) == "SBIN"
    assert fu.chain_name("NSE:SBIN26OCTFUT", fo) == "SBIN"
    assert fu.chain_name("NSE:ITC-EQ", fo) is None                     # no options listed
    assert fu.chain_name("MCX:GOLD26DECFUT", fo) is None and fu.chain_name("", fo) is None


def test_chains_come_right_after_focus_with_underlying_future_and_both_expiries():
    cfg = chain_cfg(underlyings=["SBIN"], strikes=2, expiries=2)
    plan = fu.build(cfg, [IDX], SPOT.get, T915, fo=_fo(), lists=LISTS, eq=set(), budget=5000, strat=[IDX])
    assert plan.chain_names == ["SBIN", "NIFTY"]
    cls = [plan.meta[plan.keys[s]]["cls"] for s in plan.symbols]
    assert cls[:2] == ["focus", "focus"] and set(cls[2:2 + plan.classes["chain"]]) == {"chain"}
    nifty = {k for k, n in plan.chain.items() if n == "NIFTY"}
    # the index (already focus: flagged, not re-added), its future, ATM 25000 +/- 2 on both weeklies, CE + PE
    assert {"IDX_NIFTY", "NIFTY-FUT"} <= nifty and len(nifty) == 2 + 2 * 5 * 2
    assert plan.meta["IDX_NIFTY"]["cls"] == "focus" and plan.meta["IDX_NIFTY"]["chain"] == "NIFTY"
    strikes = sorted({plan.meta[k]["strike"] for k in nifty if plan.meta[k]["kind"] in ("CE", "PE")})
    assert strikes == [24900.0, 24950.0, 25000.0, 25050.0, 25100.0]
    sbin = {k for k, n in plan.chain.items() if n == "SBIN"}
    assert {"SBIN", "SBIN-FUT"} <= sbin and len(sbin) == 2 + 5 * 2       # one listed expiry
    assert all(plan.meta[k].get("chain") == plan.chain[k] for k in plan.chain)
    assert plan.summary()["chains"] == {"names": ["SBIN", "NIFTY"], "keys": len(plan.chain)}


def test_a_chain_without_a_price_waits_but_keeps_its_underlying_and_future():
    plan = fu.build(chain_cfg(underlyings=["INFY"]), [], {}.get, T915, fo=_fo(), lists=LISTS, eq=set(), budget=5000)
    assert set(plan.chain) == {"INFY", "INFY-FUT"} and "NSE:INFY-EQ" in plan.no_price


def test_chains_survive_a_tight_budget_and_a_switched_off_universe():
    cfg = chain_cfg(strikes=1, expiries=1)
    cfg["universe"]["enabled"] = False
    plan = fu.build(cfg, [IDX], SPOT.get, T915, fo=_fo(), lists=LISTS, eq=set(), budget=5000, strat=[IDX])
    assert len(plan.chain) == 2 + 3 * 2 and plan.classes["index_opt"] == 0
    full = fu.build(chain_cfg(strikes=1, expiries=1), [IDX], SPOT.get, T915, fo=_fo(), lists=LISTS, eq=set(),
                    budget=10, strat=[IDX])
    assert len(full.chain) == 2 + 3 * 2 and full.summary()["dropped"]       # the universe was cut, not the chain


def test_the_short_list_flags_its_keys_its_chains_and_near_strikes_for_raw():
    cfg = chain_cfg(strikes=3, expiries=1)
    cfg["long"] = {**cfg["long"], "enabled": True, "symbols": ["NSE:SBIN-EQ"], "raw_strikes": 1}
    plan = fu.build(cfg, [IDX], SPOT.get, T915, fo=_fo(), lists=LISTS, eq=set(), budget=5000, strat=[IDX])
    assert plan.meta["SBIN"]["cls"] == "focus" and plan.meta["SBIN"]["long"]   # the list joins focus
    assert plan.meta["IDX_NIFTY"]["long"] and plan.meta["NIFTY-FUT"]["long"]
    assert all(plan.meta[k]["long"] for k in plan.chain)
    raw = sorted({plan.meta[k]["strike"] for k, m in plan.meta.items() if m.get("raw")})
    assert raw == [24950.0, 25000.0, 25050.0]                          # ATM +/- 1 of the NIFTY chain
    assert not any(m.get("long") for m in plan.meta.values() if m["cls"] not in ("focus", "chain", "sticky"))
    off = dict(cfg, long={**cfg["long"], "strategies": False})
    plan = fu.build(off, [IDX], SPOT.get, T915, fo=_fo(), lists=LISTS, eq=set(), budget=5000, strat=[IDX])
    assert not plan.meta["IDX_NIFTY"].get("long") and plan.meta["SBIN"]["long"]


def test_a_chains_earlier_strikes_stay_in_the_chain_while_near_the_money():
    cfg = chain_cfg(strikes=1, expiries=1)
    first = fu.build(cfg, [], SPOT.get, T915, fo=_fo(), lists=LISTS, eq=set(), budget=5000, strat=[IDX])
    moved = {**SPOT, IDX: 25110.0}
    later = fu.build(cfg, [], moved.get, T915 + 600, prev=first.opt_sel, fo=_fo(), lists=LISTS, eq=set(),
                     budget=5000, strat=[IDX])
    kept = {later.meta[k]["strike"] for k in later.chain if later.meta[k]["cls"] == "sticky"}
    assert kept and all(later.chain[k] == "NIFTY" for k in later.chain)


# ---- snapshots ---------------------------------------------------------------------------

def test_a_snapshot_holds_only_quotes_from_before_its_time(tmp_path):
    rec = chain_recorder(tmp_path)
    rec.on_tick(IDX, quote(25010.0, 0, 0, T_OPEN - 300), recv_ts=T_OPEN - 299.8)       # pre-open: arms 09:15
    feed(rec, CE, [quote(120.0, 119.5, 120.5, T_OPEN, vol=1000, oi=5e5)])               # triggers 09:15 first
    feed(rec, CE, [quote(121.0, 120.5, 121.5, T_OPEN + 30, vol=1500, oi=5.1e5)])
    feed(rec, FUT, [quote(25080.0, 25079, 25081, T_OPEN + 65, vol=9000)])               # triggers 09:16
    s = snaps(rec)
    assert set(s) == {("IDX_NIFTY", T_OPEN), ("IDX_NIFTY", T_OPEN + 60), ("NIFTY26O2725000CE", T_OPEN + 60)}
    ce = s[("NIFTY26O2725000CE", T_OPEN + 60)]
    assert ce[2:] == (T_OPEN + 30, 121.0, 120.5, 121.5, 75.0, 150.0, 5.1e5, 1500.0)    # the 09:15:30 quote
    assert s[("IDX_NIFTY", T_OPEN)][2] == T_OPEN - 300 and s[("IDX_NIFTY", T_OPEN)][9] is None   # an index: no volume
    assert ("NIFTY-FUT", T_OPEN + 60) not in s                                          # its frame came after 09:16


def test_quiet_gaps_take_one_snapshot_and_the_close_is_always_taken(tmp_path):
    rec = chain_recorder(tmp_path)
    rec.on_tick(CE, quote(120.0, 119.5, 120.5, T_OPEN - 60, vol=0), recv_ts=T_OPEN - 59)
    feed(rec, CE, [quote(120.0, 119.5, 120.5, T_OPEN + 10, vol=10)])
    feed(rec, CE, [quote(120.0, 119.5, 120.5, T_OPEN + 310, vol=20)])                   # 4 quiet minutes
    feed(rec, CE, [quote(99.0, 98.5, 99.5, T_CLOSE - 5, vol=30)])
    feed(rec, CE, [quote(98.0, 97.5, 98.5, T_CLOSE + 900, vol=40)])                     # the closing session
    feed(rec, CE, [quote(97.0, 96.5, 97.5, T_CLOSE + 1800, vol=50)])
    times = sorted(t for _, t in snaps(rec))
    assert times == [T_OPEN, T_OPEN + 300, T_CLOSE - 60, T_CLOSE]                      # no 09:16-09:19 rows
    assert snaps(rec)[("NIFTY26O2725000CE", T_CLOSE)][3] == 99.0                        # standing at 15:30
    assert rec._next_snap == T_OPEN + 86400                                              # next: tomorrow's open


def test_stale_sessions_clock_glitches_and_a_fresh_start_never_make_a_snapshot(tmp_path):
    rec = chain_recorder(tmp_path)
    rec.on_tick(CE, quote(120.0, 119.5, 120.5, T_OPEN + 1800 - 86400, vol=5), recv_ts=T_OPEN + 1800)  # yesterday's
    rec.on_tick(FUT, quote(25000.0, 24999, 25001, T_OPEN + 1800 + 900, vol=5), recv_ts=T_OPEN + 1800)  # 15 min ahead
    assert rec._next_snap == 0 and not snaps(rec)
    feed(rec, FUT, [quote(25000.0, 24999, 25001, T_OPEN + 1830, vol=10)])               # a restart at 09:45:30
    assert not snaps(rec) and rec._next_snap == T_OPEN + 1860                           # 09:45 skipped: half-quoted
    feed(rec, FUT, [quote(25001.0, 25000, 25002, T_OPEN + 1862, vol=12)])
    s = snaps(rec)
    assert set(s) == {("NIFTY-FUT", T_OPEN + 1860)}                                     # yesterday's CE quote: out
    assert s[("NIFTY-FUT", T_OPEN + 1860)][2] == T_OPEN + 1830


def test_keys_leaving_the_chain_drop_out_and_an_empty_chain_costs_nothing(tmp_path):
    rec = chain_recorder(tmp_path)
    feed(rec, CE, [quote(120.0, 119.5, 120.5, T_OPEN - 30, vol=1)])
    rec.set_chain({"NIFTY-FUT": "NIFTY"}, 60)
    assert rec._quote == {}
    rec.set_chain({}, 60)
    feed(rec, CE, [quote(121.0, 120.5, 121.5, T_OPEN + 90, vol=2)])
    assert rec._quote == {} and not snaps(rec) and rec.snap_s == 60
    rec.set_chain({"X": "NIFTY"}, 1)
    assert rec.snap_s == ticks.SNAP_MIN_S


def test_snapshots_flush_compact_and_read_back_with_their_contracts(tmp_path):
    rec = chain_recorder(tmp_path)
    rec.on_tick(IDX, quote(25010.0, 0, 0, T_OPEN - 300), recv_ts=T_OPEN - 299.8)
    feed(rec, CE, [quote(120.0, 119.5, 120.5, T_OPEN + 5, vol=1000, oi=5e5)])
    feed(rec, PE, [quote(90.0, 89.5, 90.5, T_OPEN + 20, vol=800, oi=4e5)])
    feed(rec, FUT, [quote(25080.0, 25079, 25081, T_OPEN + 61, vol=9000)])
    meta = {"IDX_NIFTY": {"symbol": IDX, "kind": "IDX", "chain": "NIFTY"},
            "NIFTY-FUT": {"symbol": FUT, "kind": "FUT", "chain": "NIFTY"},
            "NIFTY26O2725000CE": {"symbol": CE, "kind": "CE", "strike": 25000.0, "expiry": 1, "chain": "NIFTY"},
            "NIFTY26O2725000PE": {"symbol": PE, "kind": "PE", "strike": 25000.0, "expiry": 1, "chain": "NIFTY"}}
    rec.write_meta(meta, DAY0)
    assert rec.flush() >= 4
    st = ticks.FlowStore(tmp_path)
    before = st.chain_rows("NIFTY", T_OPEN - 600, T_CLOSE)
    assert [(r[0], r[1]) for r in before] == [(T_OPEN, "IDX_NIFTY"), (T_OPEN + 60, "IDX_NIFTY"),
                                               (T_OPEN + 60, "NIFTY26O2725000CE"), (T_OPEN + 60, "NIFTY26O2725000PE")]
    assert rec.compact(DAY0)
    day = tmp_path / "days" / ticks.ymd(DAY0)
    assert (day / "chain.parquet").exists() and not (tmp_path / "today" / ticks.ymd(DAY0)).exists()
    after = st.chain_rows("NIFTY", T_OPEN - 600, T_CLOSE)
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    t, rows = st.chain_at("NIFTY", T_OPEN + 90)
    assert t == T_OPEN + 60 and {r["kind"] for r in rows} == {"IDX", "CE", "PE"}
    ce = next(r for r in rows if r["kind"] == "CE")
    assert ce["strike"] == 25000.0 and ce["bid"] == 119.5 and ce["qts"] == T_OPEN + 5 and ce["symbol"] == CE
    assert st.chain_at("NIFTY", T_OPEN - 400) == (None, [])
    assert st.chain_at("BANKNIFTY", T_OPEN + 90) == (None, [])


def test_flags_set_later_in_the_day_are_kept_and_survive_compaction(tmp_path):
    rec = ticks.TickRecorder(tmp_path)
    rec.write_meta({"SBIN": {"symbol": "NSE:SBIN-EQ", "cls": "stocks"}}, DAY0)
    rec.write_meta({"SBIN": {"symbol": "NSE:SBIN-EQ", "cls": "focus", "long": True},
                    "INFY": {"symbol": "NSE:INFY-EQ", "cls": "stocks"}}, DAY0)
    m = ticks.read_json(tmp_path / "today" / ticks.ymd(DAY0) / "meta.json")
    assert m["SBIN"] == {"symbol": "NSE:SBIN-EQ", "cls": "stocks", "long": True}         # first contract, flag added
    out = tmp_path / "days" / ticks.ymd(DAY0)
    ticks.write_json(out / "meta.json", {"SBIN": {"symbol": "NSE:SBIN-EQ", "cls": "stocks"}})
    assert rec.compact(DAY0)
    assert ticks.read_json(out / "meta.json")["SBIN"]["long"] is True


# ---- tiered retention ---------------------------------------------------------------------

def make_day(root, day, keys, long_keys, raw=True):
    import duckdb

    d = root / "days" / ticks.ymd(day)
    d.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    m0 = day * 86400 - IST + 33300
    vals = ", ".join(f"('{k}', {m0 + 60 * i})" for k in keys for i in (2, 0, 1))      # deliberately unsorted
    con.execute(f"CREATE TABLE r(key VARCHAR, t BIGINT); INSERT INTO r VALUES {vals}")
    con.execute(f"COPY (SELECT key, t AS minute, 1.0 AS buy, 2.0 AS sell FROM r) TO '{d / 'flow.parquet'}' (FORMAT parquet)")
    con.execute(f"COPY (SELECT key, t AS minute, 100.0 AS price, 1.0 AS buy, 0.0 AS sell FROM r) "
                f"TO '{d / 'fp.parquet'}' (FORMAT parquet)")
    con.execute(f"COPY (SELECT key, t AS ts, 100.0 AS price, 5.0 AS qty, 1 AS side FROM r) TO '{d / 'prints.parquet'}' (FORMAT parquet)")
    con.execute(f"COPY (SELECT key, t AS ts, t AS qts, 1.0 AS bid FROM r) TO '{d / 'chain.parquet'}' (FORMAT parquet)")
    if raw:
        con.execute(f"COPY (SELECT key, t AS exch_ts, 100.0 AS ltp FROM r) TO '{d / 'raw.parquet'}' (FORMAT parquet)")
    ticks.write_json(d / "meta.json", {k: {"symbol": k, **({"long": True} if k in long_keys else {})} for k in keys})
    return d


def keys_in(path):
    import duckdb

    return [tuple(r) for r in duckdb.connect().execute(f"SELECT * FROM read_parquet('{path}')").fetchall()]


LONG = {"enabled": True, "days": 5, "raw_days": 3}


def test_with_the_short_list_off_pruning_is_unchanged(tmp_path):
    rec = ticks.TickRecorder(tmp_path)
    days = [ticks.ymd(DAY0 - 100 + i) for i in range(91)]
    for d in days:
        (tmp_path / "days" / d).mkdir(parents=True)
        (tmp_path / "days" / d / "flow.parquet").write_bytes(b"x")
        (tmp_path / "days" / d / "raw.parquet").write_bytes(b"x")
    assert rec.prune(90, 5, {**LONG, "enabled": False}) == 1 + 85
    assert sorted(p.name for p in (tmp_path / "days").iterdir()) == days[1:]
    assert sorted(p.parent.name for p in tmp_path.glob("days/*/raw.parquet")) == days[-5:]


def test_days_leaving_the_window_keep_only_the_short_list_then_go(tmp_path):
    rec = ticks.TickRecorder(tmp_path)
    ds = [make_day(tmp_path, DAY0 - 6 + i, ["A", "B", "C"], {"A"}) for i in range(7)]       # ages 6 .. 0
    nolong = make_day(tmp_path, DAY0 - 3, ["B", "C"], set())                                 # replaces age 3's
    ds[3] = nolong
    rec.prune(2, 1, LONG)                                       # window 2 days, raw 1; list 5 days, its raw 3
    by_age = {6 - i: d for i, d in enumerate(ds)}
    assert not by_age[6].exists() and not by_age[5].exists()   # past long.days
    assert not by_age[3].exists()                               # left the window with nothing on the list
    for age in (4, 2):
        d = by_age[age]
        assert ticks.read_json(d / "trim.json")["keys"] == 1
        for name in ("flow.parquet", "fp.parquet", "prints.parquet", "chain.parquet"):
            assert {r[0] for r in keys_in(d / name)} == {"A"}
    assert {r[0] for r in keys_in(by_age[2] / "raw.parquet")} == {"A"}       # age 2 < long.raw_days
    assert not (by_age[4] / "raw.parquet").exists()                          # age 4: raw gone
    assert {r[0] for r in keys_in(by_age[1] / "flow.parquet")} == {"A", "B", "C"}   # in the window: whole
    assert {r[0] for r in keys_in(by_age[1] / "raw.parquet")} == {"A"}       # but raw past raw_days: the list's
    assert {r[0] for r in keys_in(by_age[0] / "raw.parquet")} == {"A", "B", "C"}
    rows = [r[1] for r in keys_in(by_age[2] / "flow.parquet")]
    assert rows == sorted(rows)                                              # trimmed files stay in key, time order
    st = ticks.FlowStore(tmp_path)
    assert st.long_days() == [by_age[4].name, by_age[2].name]
    stamp = {p: p.stat().st_mtime_ns for p in tmp_path.glob("days/*/*")}
    assert rec.prune(2, 1, LONG) == 0                                        # idempotent
    assert {p: p.stat().st_mtime_ns for p in tmp_path.glob("days/*/*")} == stamp


def test_switching_the_short_list_off_lets_the_trimmed_days_go(tmp_path):
    rec = ticks.TickRecorder(tmp_path)
    for i in range(4):
        make_day(tmp_path, DAY0 - 3 + i, ["A", "B"], {"A"})
    rec.prune(2, 2, LONG)
    assert len(ticks.FlowStore(tmp_path).long_days()) == 2
    rec.prune(2, 2, {**LONG, "enabled": False})
    assert sorted(p.name for p in (tmp_path / "days").iterdir()) == [ticks.ymd(DAY0 - 1), ticks.ymd(DAY0)]


def test_a_shorter_long_raw_window_drops_raw_from_already_trimmed_days(tmp_path):
    rec = ticks.TickRecorder(tmp_path)
    for i in range(4):
        make_day(tmp_path, DAY0 - 3 + i, ["A", "B"], {"A"})
    rec.prune(1, 0, {**LONG, "raw_days": 4})
    old = tmp_path / "days" / ticks.ymd(DAY0 - 3)
    assert ticks.read_json(old / "trim.json")["raw_trimmed"] is True
    rec.prune(1, 0, {**LONG, "raw_days": 2})
    assert not (old / "raw.parquet").exists() and ticks.read_json(old / "trim.json")["raw_trimmed"] is False


# ---- capacity --------------------------------------------------------------------------------

def test_capacity_projects_each_tier_from_real_day_sizes(tmp_path):
    for i in range(3):
        make_day(tmp_path, DAY0 - 2 + i, ["A", "B", "C", "D"], {"A"})
    cfg = ticks.load_config(tmp_path)
    cfg.update(retention_days=90, raw_days=5)
    cap = ticks.capacity(cfg, tmp_path)
    full = sum(p.stat().st_size for p in (tmp_path / "days" / ticks.ymd(DAY0)).glob("*.parquet")
               if p.name != "raw.parquet")
    assert cap["per_day_mb"]["full"] == round(full / 1e6, 2) and cap["tiers_gb"]["long"] == 0
    assert cap["fits"] and cap["note"] is None and cap["estimate"] == []
    cfg["long"] = {**cfg["long"], "enabled": True, "days": 365, "raw_days": 120}
    est = ticks.capacity(cfg, tmp_path)
    assert est["estimate"] == ["long", "long_raw"] and est["days"]["long"] == 365
    assert est["per_day_mb"]["long"] == pytest.approx(round(full / 4 / 1e6, 2), abs=0.01)   # 1 of 4 keys
    ticks.TickRecorder(tmp_path).prune(1, 0, {**cfg["long"], "raw_days": 3})
    ticks._CAP.update(t=0.0)
    measured = ticks.capacity(cfg, tmp_path)
    assert measured["basis_days"]["long"] == 2 and "long" not in measured["estimate"]
    cfg["min_free_gb"] = 1e6
    assert ticks.capacity(cfg, tmp_path)["fits"] is False                     # the guard's floor is the limit


def test_capacity_with_nothing_stored_says_so(tmp_path):
    cap = ticks.capacity(ticks.load_config(tmp_path), tmp_path)
    assert cap["fits"] is False and "no stored day" in cap["note"]


# ---- the collector ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_collector_records_strategy_chains_and_flags_the_day(tmp_path, monkeypatch):
    from app.algo import flowd

    ticks.save_config({"symbols": [], "budget": 400, "universe": {"lists": ["nifty500"]},
                       "chains": {"enabled": True, "strikes": 2, "expiries": 1, "interval_s": 30},
                       "long": {"enabled": True, "raw_strikes": 0}}, tmp_path)
    (tmp_path / "strategy_symbols.json").write_text(json.dumps([IDX]))
    monkeypatch.setattr(fu, "contracts", lambda *a, **k: _fo())
    monkeypatch.setattr(fu, "index_list", lambda name, cache_dir, fetch=True: (["RELIANCE", "SBIN", "INFY"], "test"))
    monkeypatch.setattr(flowd, "_account", lambda: None)
    c = flowd.Collector(tmp_path)
    c.rec.ltp.update({k.upper(): v for k, v in SPOT.items()})
    await c.replan(T915)
    assert len(c.rec.chain_keys) == 2 + 5 * 2 and c.rec.snap_s == 30
    atm = next(k for k in c.rec.chain_keys if k.endswith("25000CE"))
    assert atm in c.rec.raw_keys and "IDX_NIFTY" in c.rec.raw_keys        # raw: focus + the ATM strike
    meta = ticks.read_json(tmp_path / "today" / ticks.ymd(DAY0) / "meta.json")
    assert meta["IDX_NIFTY"]["chain"] == "NIFTY" and meta["IDX_NIFTY"]["long"] and meta[atm]["raw"]
    plan = ticks.read_json(tmp_path / "plan.json")
    assert plan["chains"]["names"] == ["NIFTY"] and plan["classes"]["chain"] == 5 * 2          # the index + its future are already focus
    c.write_status()
    assert ticks.read_json(tmp_path / "status.json")["chains"]["keys"] == len(c.rec.chain_keys)


# ---- the API ----------------------------------------------------------------------------------

def _api_root(monkeypatch, tmp_path):
    from app.tests.test_orderflow_collector import _orig_load, _orig_save

    monkeypatch.setattr(ticks, "ROOT", tmp_path)
    monkeypatch.setattr(ticks, "_STORE", ticks.FlowStore(tmp_path))
    monkeypatch.setattr(ticks, "load_config", lambda root=tmp_path: _orig_load(tmp_path))
    monkeypatch.setattr(ticks, "save_config", lambda patch, root=tmp_path: _orig_save(patch, tmp_path))
    monkeypatch.setattr(ticks, "strategy_symbols", lambda root=tmp_path: [])
    real_cap = ticks.capacity
    monkeypatch.setattr(ticks, "capacity", lambda cfg, root=tmp_path, max_age_s=300.0: real_cap(cfg, tmp_path, 0))


def test_the_config_api_validates_chains_and_the_short_list(client, tmp_path, monkeypatch):
    _api_root(monkeypatch, tmp_path)
    for bad in ({"chains": {"strikes": 99}}, {"chains": {"interval_s": 5}}, {"chains": {"underlyings": "NIFTY"}},
                {"chains": {"enabled": "yes"}}, {"chains": {"depth": 3}}, {"chains": []},
                {"long": {"days": 5000}}, {"long": {"raw_days": -1}}, {"long": {"symbols": [1]}},
                {"long": {"raw_strikes": True}}):
        assert client.put("/api/algo/ticks/config", json=bad).status_code == 422, bad
    ok = client.put("/api/algo/ticks/config", json={
        "chains": {"enabled": True, "underlyings": ["nifty", "banknifty"], "strikes": 12, "expiries": 3, "interval_s": 30},
        "long": {"enabled": True, "symbols": ["NIFTY:FUT"], "days": 500, "raw_days": 200, "raw_strikes": 2}})
    assert ok.status_code == 200
    cfg = ok.json()["config"]
    assert cfg["chains"] == {"enabled": True, "underlyings": ["NIFTY", "BANKNIFTY"], "strategies": True, "strikes": 12,
                             "expiries": 3, "interval_s": 30}
    assert cfg["long"]["days"] == 500 and cfg["long"]["symbols"] == ["NIFTY:FUT"]
    st = ok.json()
    assert "capacity" in st and st["days"]["long"] == {"count": 0, "first": None}


def test_the_status_splits_full_and_short_list_days(client, tmp_path, monkeypatch):
    _api_root(monkeypatch, tmp_path)
    for i in range(4):
        make_day(tmp_path, DAY0 - 3 + i, ["A", "B"], {"A"})
    ticks.TickRecorder(tmp_path).prune(2, 2, LONG)
    st = client.get("/api/algo/ticks/status").json()
    assert st["days"]["count"] == 2 and st["days"]["long"] == {"count": 2, "first": ticks.ymd(DAY0 - 3)}
    assert st["capacity"]["basis_days"]["long"] == 2


def test_the_chain_api_returns_the_snapshot_at_a_time(client, tmp_path, monkeypatch):
    _api_root(monkeypatch, tmp_path)
    rec = chain_recorder(tmp_path)
    rec.on_tick(IDX, quote(25010.0, 0, 0, T_OPEN - 300), recv_ts=T_OPEN - 299.8)
    feed(rec, CE, [quote(120.0, 119.5, 120.5, T_OPEN + 5, vol=1000, oi=5e5)])
    feed(rec, FUT, [quote(25080.0, 25079, 25081, T_OPEN + 61, vol=9000)])
    rec.write_meta({"IDX_NIFTY": {"symbol": IDX, "kind": "IDX", "chain": "NIFTY"},
                    "NIFTY-FUT": {"symbol": FUT, "kind": "FUT", "chain": "NIFTY"},
                    "NIFTY26O2725000CE": {"symbol": CE, "kind": "CE", "strike": 25000.0, "expiry": 1,
                                          "chain": "NIFTY"}}, DAY0)
    rec.flush()
    r = client.get("/api/algo/ticks/chain", params={"name": "nifty", "at": T_OPEN + 70}).json()
    assert r["ts"] == T_OPEN + 60 and r["count"] == 2
    assert [x["kind"] for x in r["rows"]] == ["IDX", "CE"]
    ce = r["rows"][1]
    assert ce["mid"] == 120.0 and ce["spread"] == 1.0 and r["rows"][0]["mid"] is None
    assert client.get("/api/algo/ticks/chain", params={"name": "NIFTY; DROP"}).status_code == 422
    assert client.get("/api/algo/ticks/chain", params={"name": "SBIN", "at": T_OPEN + 70}).json()["count"] == 0
    assert time.time() > 0
