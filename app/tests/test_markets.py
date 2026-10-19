"""Other markets: crypto from Binance's public feed (watch only), MCX / currency from the Fyers masters."""
from __future__ import annotations

import asyncio

from app.services import crypto
from app.services.instrument_master import InstrumentMaster

T0 = 1_792_000_000 - 1_792_000_000 % 3600          # an hour boundary (UTC grid)


def _k(t: int, o: float, h: float, l: float, c: float, v: float, tb: float) -> list:
    return [t * 1000, str(o), str(h), str(l), str(c), str(v), t * 1000 + 59_999, "0", 1, str(tb), "0", "0"]


def test_klines_build_missing_resolutions_on_the_utc_grid_with_taker_buy_flow(monkeypatch):
    rows = [_k(T0 + i * 300, 100 + i, 101 + i, 99 + i, 100.5 + i, 10, 6 + i % 2) for i in range(4)]   # four 5m bars

    async def fake(path, params):
        assert path == "klines" and params["interval"] == "5m" and params["symbol"] == "BTCUSDT"
        return rows
    monkeypatch.setattr(crypto, "_get", fake)
    got = asyncio.run(crypto.klines("CRYPTO:BTCUSDT", "10", T0, T0 + 3600))   # 10m = two 5m bars each
    assert [r[0] for r in got] == [T0, T0 + 600]
    t, o, h, l, c, v, tb = got[0]
    assert (o, h, l, c, v, tb) == (100.0, 102.0, 99.0, 101.5, 20.0, 13.0)       # open of the first, close of the last


def test_klines_walk_back_so_a_long_range_keeps_its_newest_candles(monkeypatch):
    calls = []

    async def fake(path, params):
        calls.append(params["endTime"])
        end = params["endTime"] // 1000
        return [_k(end - (crypto.PAGE - i) * 60, 1, 1, 1, 1, 1, 0.5) for i in range(crypto.PAGE)]   # always a full page
    monkeypatch.setattr(crypto, "_get", fake)
    monkeypatch.setattr(crypto, "MAX_PAGES", 2)
    got = asyncio.run(crypto.klines("CRYPTO:ETHUSDT", "1", T0 - 10 * 86400, T0))
    assert len(calls) == 2 and got[-1][0] == T0 - 60 and len(got) == 2 * crypto.PAGE   # the newest kept, oldest dropped


def test_crypto_rejects_unknown_pairs_and_second_candles():
    for sym, res in (("CRYPTO:FAKEUSDT", "5"), ("CRYPTO:BTCUSDT", "5S")):
        try:
            asyncio.run(crypto.klines(sym, res, T0, T0 + 60))
        except ValueError:
            continue
        raise AssertionError(f"{sym} {res} accepted")


def test_live_feed_payload_matches_the_quotes_channel_and_throttles():
    f = crypto.Feed()
    tick = {"data": {"e": "24hrTicker", "s": "BTCUSDT", "x": "80000", "b": "81999", "a": "82001", "c": "82000", "E": 1_792_000_000_000}}
    q = f.on_message(tick)
    assert q["symbol"] == "CRYPTO:BTCUSDT" and q["last_price"] == 82000 and q["change_pct"] == 2.5 and q["source"] == "binance"
    trade = lambda p, qty: {"data": {"e": "aggTrade", "s": "BTCUSDT", "p": str(p), "q": str(qty), "T": 1_792_000_001_000}}  # noqa: E731
    f.last_pub["BTCUSDT"] = 0.0
    q = f.on_message(trade(82010, 0.5))
    assert q["last_price"] == 82010 and q["volume"] == 0.5
    assert f.on_message(trade(82020, 0.25)) is None                     # inside the throttle window: held back
    f.last_pub["BTCUSDT"] = 0.0
    assert f.on_message(trade(82030, 0.25))["volume"] == 1.0            # but its volume is counted
    assert f.on_message({"data": {"e": "kline", "s": "BTCUSDT"}}) is None


def test_mcx_and_currency_rows_keep_their_segment(tmp_path):
    def row(sym, name, seg, opt="XX", strike="-1.0", exp="1796407200"):
        short = sym.split(":")[1].split("26")[0]
        return ",".join(["1", name, "30", "1", "1.0", "", "0900-2330|1815-1915:", "2026-10-17", exp, sym, "11", seg,
                         "1", short, "114", strike, opt, "1", exp, "0", "0.0"])
    (tmp_path / "MCX_COM.csv").write_text("\n".join([
        row("MCX:GOLD26DECFUT", "GOLD 04 Dec 26 FUT", "20"),
        row("MCX:GOLD26DEC130000CE", "GOLD 130000 CE", "20", "CE", "130000"),
        row("NSE:USDINR26OCTFUT", "USDINR 28 Oct 26 FUT", "12")]) + "\n", encoding="utf-8")
    m = InstrumentMaster(data_dir=tmp_path)
    assert m.get("MCX:GOLD26DECFUT").segment == "COM" and m.get("NSE:USDINR26OCTFUT").segment == "CD"
    assert m.get("MCX:GOLD26DEC130000CE").segment == "COM"             # options too
    assert m.get("CRYPTO:BTCUSDT").exchange == "CRYPTO"                # crypto pairs are always listed


def test_orders_refuse_crypto_mcx_and_currency(client):
    from app.api.orders import tradable_market

    assert not tradable_market("CRYPTO:BTCUSDT") and not tradable_market("MCX:GOLD26DECFUT")
    assert tradable_market("NSE:SBIN-EQ")
    r = client.post("/api/orders", json={"account_id": -1, "symbol": "CRYPTO:BTCUSDT", "side": "BUY", "quantity": 1,
                                         "order_type": "MARKET"})
    assert r.status_code == 422 and "charts and data only" in r.json()["detail"]


def test_history_quotes_and_flow_route_crypto_to_binance(client, monkeypatch):
    async def fake(path, params):
        if path == "klines":
            return [_k(T0, 100, 101, 99, 100.5, 10, 7)]
        return [{"symbol": "BTCUSDT", "lastPrice": "82000", "priceChange": "500", "priceChangePercent": "0.61"}]
    monkeypatch.setattr(crypto, "_get", fake)
    h = client.get(f"/api/market/history?symbol=CRYPTO:BTCUSDT&resolution=5&from={T0 - 60}&to={T0 + 60}").json()
    assert h["ok"] and h["candles"] == [[T0, 100.0, 101.0, 99.0, 100.5, 10.0]]
    f = client.get(f"/api/algo/ticks/flow?symbol=CRYPTO:BTCUSDT&resolution=5&from={T0 - 60}&to={T0 + 60}&footprint=true").json()
    assert f["bars"] == [[T0, 7.0, 3.0, 4.0]] and f["footprint"] == []    # taker buys 7 of 10
    q = client.get("/api/market/quotes?symbols=CRYPTO:BTCUSDT").json()["quotes"]
    assert q["CRYPTO:BTCUSDT"]["ltp"] == 82000
