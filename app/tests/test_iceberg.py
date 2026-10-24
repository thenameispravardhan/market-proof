"""Likely icebergs from raw frames (app/algo/iceberg.py) and the API that serves them."""
from __future__ import annotations

from app.algo import iceberg, ticks
from app.tests.test_orderflow_collector import DAY0, T915, feed, frame


def _f(ts, ltp, dv, side, bid, ask, bsz, asz):
    # RAW_COLS: recv_ts, exch_ts, ltp, vol, dv, side, bid, ask, bid_size, ask_size, tot_buy, tot_sell, ltq, oi
    return (ts + 0.2, ts, ltp, 0, dv, side, bid, ask, bsz, asz, 0, 0, 0, None)


def test_an_offer_that_keeps_refilling_while_buyers_lift_it_is_a_sell_iceberg():
    frames = [
        _f(1, 100.05, 50, 1, 100.0, 100.05, 400, 75),     # buyers lift 100.05; the offer still shows 75
        _f(2, 100.05, 60, 1, 100.0, 100.05, 400, 20),     # traded down to 20 shown ...
        _f(3, 100.05, 70, 1, 100.0, 100.05, 400, 75),     # ... refilled to 75
        _f(4, 100.05, 80, 1, 100.0, 100.05, 400, 75),
        _f(5, 100.10, 10, 1, 100.05, 100.10, 300, 50),    # the offer finally gives way: the episode ends
    ]
    (ice,) = iceberg.detect(frames, min_ratio=2.0)
    assert (ice["side"], ice["price"], ice["start"], ice["end"]) == ("SELL", 100.05, 1, 4)
    assert (ice["traded"], ice["shown"], ice["refills"], ice["ratio"]) == (260, 75, 1, 3.47)


def test_a_bid_that_absorbs_sellers_is_a_buy_iceberg_and_an_ordinary_order_is_not():
    absorb = [_f(1, 99.95, 100, -1, 99.95, 100.0, 50, 60), _f(2, 99.95, 100, -1, 99.95, 100.0, 50, 60),
              _f(3, 99.95, 100, -1, 99.95, 100.0, 10, 60), _f(4, 99.95, 100, -1, 99.95, 100.0, 50, 60),
              _f(5, 99.90, 20, -1, 99.90, 99.95, 30, 40)]
    assert [i["side"] for i in iceberg.detect(absorb)] == ["BUY"]
    plain = [_f(1, 100.05, 50, 1, 100.0, 100.05, 400, 300), _f(2, 100.05, 60, 1, 100.0, 100.05, 400, 240),
             _f(3, 100.10, 10, 1, 100.05, 100.10, 300, 50)]          # traded 110 of a 300-lot offer: no iceberg
    assert iceberg.detect(plain) == []


def test_the_api_reads_the_futures_raw_frames_for_an_index(client, tmp_path, monkeypatch):
    rec = ticks.TickRecorder(tmp_path)
    monkeypatch.setattr(ticks, "_STORE", ticks.FlowStore(tmp_path))
    fut = "NSE:NIFTY26OCTFUT"
    rec.key_of[fut] = "NIFTY-FUT"
    rec.raw_keys = {"NIFTY-FUT"}
    vol = 0
    frames = [frame(25100.0, vol, 25099.0, 25100.0, T915, bid_size=200, ask_size=75)]
    for i, size in enumerate((40, 75, 30, 75, 75)):                   # buyers lift 25100, the offer refills
        vol += 100
        frames.append(frame(25100.0, vol, 25099.0, 25100.0, T915 + 5 + i, bid_size=200, ask_size=size))
    frames.append(frame(25100.5, vol + 10, 25100.0, 25100.5, T915 + 20, bid_size=200, ask_size=50))
    feed(rec, fut, frames)
    rec.flush()
    rec.write_meta({"NIFTY-FUT": {"symbol": fut, "kind": "FUT"}}, DAY0)
    body = client.get("/api/algo/ticks/icebergs", params={"symbol": "NSE:NIFTY50-INDEX", "from": T915 - 60,
                                                           "to": T915 + 600}).json()
    assert body["key"] == "NIFTY-FUT" and body["recorded"]
    (ice,) = body["icebergs"]
    assert ice["side"] == "SELL" and ice["price"] == 25100.0 and ice["traded"] >= 2 * ice["shown"]
    assert client.get("/api/algo/ticks/icebergs", params={"symbol": "NIFTY", "from": T915,
                                                           "to": T915 + 8 * 86400}).status_code == 422
