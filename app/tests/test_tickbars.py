"""Bars and footprints from raw frames (app/algo/tickbars.py) and the endpoints serving them."""
from __future__ import annotations

import pytest

from app.algo import tickbars, ticks
from app.tests.test_orderflow_collector import DAY0, T915, feed, frame


def _f(recv, ts, ltp, dv, side, oi=None, tb=0, ts_=0):
    # RAW_COLS: recv_ts, exch_ts, ltp, vol, dv, side, bid, ask, bid_size, ask_size, tot_buy, tot_sell, ltq, oi
    return (recv, ts, ltp, 0, dv, side, 0, 0, 0, 0, tb, ts_, 0, oi)


def test_trades_classify_like_the_recorder_and_keep_gaps_and_the_auction_apart():
    tr = tickbars.trades([
        _f(T915 - 400, T915 - 400, 100.0, 500, 0),        # the pre-open match: unk
        _f(T915 + 1, T915 + 1, 100.1, 10, 1),             # buy (the quiet since the pre-open is no gap)
        _f(T915 + 2, T915 + 2, 100.0, 6, 0),              # no side: half / half
        _f(T915 + 200, T915 + 200, 100.2, 99, 1),         # 198 s of silence inside the session: a feed gap, unk
        _f(T915 + 201, T915 + 201, 100.2, 0, 1),          # nothing traded: no trade
    ])
    assert [(t.buy, t.sell, t.unk) for t in tr] == [(0, 0, 500), (10, 0, 0), (3, 3, 0), (0, 0, 99)]


def test_time_bars_of_seconds_with_a_footprint():
    tr = tickbars.trades([_f(T915 + 1, T915 + 1, 100.0, 10, 1, oi=5000, tb=900, ts_=300),
                          _f(T915 + 3, T915 + 3, 100.05, 4, -1, oi=5010, tb=700, ts_=500),
                          _f(T915 + 6, T915 + 6, 100.1, 7, 1)])
    bars, fp = tickbars.time_bars(tr, 5)
    assert [b[0] for b in bars] == [T915, T915 + 5]
    _t, buy, sell, delta, n, _unk, oi, close, dlo, dhi, bid, ask = bars[0]
    assert (buy, sell, delta, n, oi, close, dlo, dhi, bid, ask) == (10, 4, 6, 2, 5010, 100.05, 0, 10, 800, 400)
    assert fp == [[T915, 100.0, 10, 0], [T915, 100.05, 0, 4], [T915 + 5, 100.1, 7, 0]]


@pytest.mark.parametrize("kind,size,expect_closes", [
    ("range", 0.2, [100.2, 100.25]),      # bar 1 spans 100.0-100.2; 100.25 opens bar 2; 99.95 is 0.3 below it: bar 3
    ("volume", 15, [100.1, 100.25]),      # bars of >= 15 classified quantity
    ("tick", 2, [100.1, 100.25]),         # bars of 2 trades
])
def test_tick_bars_close_on_range_volume_or_trade_count(kind, size, expect_closes):
    frames = [_f(T915 + i, T915 + 1, px, 8, 1) for i, px in enumerate((100.0, 100.1, 100.2, 100.25, 99.95))]
    bars, fp = tickbars.tick_bars(tickbars.trades(frames), kind, size)
    assert [b["close"] for b in bars[:2]] == expect_closes
    assert [b["t"] for b in bars] == sorted({b["t"] for b in bars})          # one bar per second, increasing
    assert sum(r[2] for r in fp) == sum(b["buy"] for b in bars) == 40
    with pytest.raises(ValueError):
        tickbars.tick_bars([], "renko", 1)


def _recorded(tmp_path, monkeypatch):
    rec = ticks.TickRecorder(tmp_path)
    monkeypatch.setattr(ticks, "_STORE", ticks.FlowStore(tmp_path))
    fut = "NSE:NIFTY26OCTFUT"
    rec.key_of[fut] = "NIFTY-FUT"
    rec.raw_keys = {"NIFTY-FUT"}
    frames, vol = [frame(25100.0, 0, 25099.0, 25100.0, T915)], 0
    for i, px in enumerate((25100.0, 25100.5, 25101.0, 25099.0, 25098.5)):
        vol += 10
        bid, ask = (px - 0.5, px) if i < 3 else (px, px + 0.5)          # lifted then hit
        frames.append(frame(px, vol, bid, ask, T915 + 2 + 3 * i))
    feed(rec, fut, frames)
    rec.flush()
    rec.write_meta({"NIFTY-FUT": {"symbol": fut, "kind": "FUT"}}, DAY0)
    return fut


def test_the_flow_api_serves_seconds_from_raw_frames_and_falls_back_to_minutes(client, tmp_path, monkeypatch):
    fut = _recorded(tmp_path, monkeypatch)
    body = client.get("/api/algo/ticks/flow", params={"symbol": "NSE:NIFTY50-INDEX", "resolution": "5S", "from": T915,
                                                       "to": T915 + 60, "footprint": "true"}).json()
    assert body["source"] == "raw" and body["key"] == "NIFTY-FUT"
    assert [b[0] for b in body["bars"]] == [T915, T915 + 5, T915 + 10]               # prints at +2 … +14 s
    assert sum(b[1] for b in body["bars"]) == 30 and sum(b[2] for b in body["bars"]) == 20
    assert {r[1] for r in body["footprint"]} == {25100.0, 25100.5, 25101.0, 25099.0, 25098.5}
    other = client.get("/api/algo/ticks/flow", params={"symbol": "NSE:SBIN-EQ", "resolution": "15S", "from": T915,
                                                        "to": T915 + 60}).json()
    assert other["source"] == "minute"                                    # no raw frames: the minute flow
    assert client.get("/api/algo/ticks/flow", params={"symbol": fut, "resolution": "90S", "from": T915,
                                                       "to": T915 + 60}).status_code == 422


def test_the_tickbars_api(client, tmp_path, monkeypatch):
    fut = _recorded(tmp_path, monkeypatch)
    body = client.get("/api/algo/ticks/tickbars", params={"symbol": fut, "kind": "range", "size": 1.0, "from": T915,
                                                           "to": T915 + 600}).json()
    assert body["recorded"] and body["key"] == "NIFTY-FUT"
    assert [(b["open"], b["high"], b["low"], b["close"]) for b in body["bars"]] == [
        (25100.0, 25101.0, 25100.0, 25101.0), (25099.0, 25099.0, 25098.5, 25098.5)]
    assert client.get("/api/algo/ticks/tickbars", params={"symbol": fut, "kind": "renko", "size": 1, "from": T915,
                                                           "to": T915 + 600}).status_code == 422
