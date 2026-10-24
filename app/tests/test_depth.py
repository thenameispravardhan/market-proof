"""The order book: recorded from DepthUpdate frames (sampled), compacted, pruned, served as a heatmap."""
from __future__ import annotations

from app.algo import ticks
from app.tests.test_orderflow_collector import DAY0, T915


def _book(bid, ask, bq=100, aq=200):
    return {"bids": [[bid - 0.5 * i, bq * (i + 1), 1] for i in range(5)],
            "asks": [[ask + 0.5 * i, aq * (i + 1), 1] for i in range(5)]}


def test_depth_is_sampled_per_interval_flushed_compacted_and_pruned(tmp_path):
    rec = ticks.TickRecorder(tmp_path)
    rec.depth_keys = {"NSE:NIFTY26OCTFUT": "NIFTY-FUT"}
    rec.on_depth("NSE:NIFTY26OCTFUT", _book(25100, 25100.5), recv_ts=T915)
    rec.on_depth("NSE:NIFTY26OCTFUT", _book(25101, 25101.5), recv_ts=T915 + 0.4)   # inside the second: dropped
    rec.on_depth("NSE:NIFTY26OCTFUT", _book(25102, 25102.5), recv_ts=T915 + 1.2)
    rec.on_depth("NSE:SBIN-EQ", _book(800, 800.05), recv_ts=T915)                   # not on the list
    rec.flush()
    st = ticks.FlowStore(tmp_path)
    rows = st.depth_rows("NIFTY-FUT", T915, T915 + 60)
    assert [(r[0], r[1], r[2], r[11], r[12]) for r in rows] == [(T915, 25100, 100, 25100.5, 200), (T915 + 1, 25102, 100, 25102.5, 200)]
    assert rec.compact(DAY0)
    assert [r[1] for r in ticks.FlowStore(tmp_path).depth_rows("NIFTY-FUT", T915, T915 + 60)] == [25100, 25102]   # parquet
    assert (tmp_path / "days" / ticks.ymd(DAY0) / "depth.parquet").exists()
    (tmp_path / "days" / ticks.ymd(DAY0 + 1)).mkdir(parents=True)
    (tmp_path / "days" / ticks.ymd(DAY0 + 1) / "flow.parquet").write_bytes(b"")
    rec.prune(90, 5, None, depth_days=1)                                            # one newer day: this one is out
    assert not (tmp_path / "days" / ticks.ymd(DAY0) / "depth.parquet").exists()


def test_the_depth_heatmap_is_time_weighted_per_candle(client, tmp_path, monkeypatch):
    rec = ticks.TickRecorder(tmp_path)
    monkeypatch.setattr(ticks, "_STORE", ticks.FlowStore(tmp_path))
    fut = "NSE:NIFTY26OCTFUT"
    rec.depth_keys = {fut: "NIFTY-FUT"}
    rec.on_depth(fut, _book(25100, 25100.5, bq=100), recv_ts=T915)          # stands 45 s
    rec.on_depth(fut, _book(25100, 25100.5, bq=400), recv_ts=T915 + 45)     # then 15 s (to the next sample)
    rec.on_depth(fut, _book(25100, 25100.5, bq=400), recv_ts=T915 + 60)
    rec.flush()
    rec.write_meta({"NIFTY-FUT": {"symbol": fut, "kind": "FUT"}}, DAY0)
    body = client.get("/api/algo/ticks/depth", params={"symbol": "NSE:NIFTY50-INDEX", "resolution": "1",
                                                        "from": T915, "to": T915 + 60}).json()
    assert body["key"] == "NIFTY-FUT" and body["recorded"]
    cell = next(c for c in body["cells"] if c[0] == T915 and c[1] == 25100.0)
    assert cell[2] == (45 * 100 + 15 * 400) / 60                            # 175: time-weighted, not 250
    assert next(c for c in body["cells"] if c[1] == 25100.5)[3] == 200
    assert client.get("/api/algo/ticks/depth", params={"symbol": fut, "resolution": "x", "from": T915,
                                                        "to": T915 + 60}).status_code == 422


def test_depth_config_is_validated_and_saved(client, tmp_path, monkeypatch):
    from app.tests.test_orderflow_collector import _orig_load, _orig_save

    monkeypatch.setattr(ticks, "ROOT", tmp_path)
    monkeypatch.setattr(ticks, "load_config", lambda root=tmp_path: _orig_load(tmp_path))
    monkeypatch.setattr(ticks, "save_config", lambda patch, root=tmp_path: _orig_save(patch, tmp_path))
    monkeypatch.setattr(ticks, "strategy_symbols", lambda root=tmp_path: [])
    assert client.put("/api/algo/ticks/config", json={"depth": {"interval_s": 0}}).status_code == 422
    ok = client.put("/api/algo/ticks/config", json={"depth": {"days": 3, "symbols": ["nse:sbin-eq"]}}).json()["config"]
    assert ok["depth"] == {"enabled": True, "symbols": ["NSE:SBIN-EQ"], "interval_s": 1, "days": 3}
