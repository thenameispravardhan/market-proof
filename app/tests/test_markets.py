"""Other markets: crypto from Binance's public feed (watch only), MCX / currency from the Fyers masters."""
from __future__ import annotations

import asyncio

import pytest

from app.services import crypto
from app.services.instrument_master import InstrumentMaster

T0 = 1_792_000_000 - 1_792_000_000 % 3600          # an hour boundary (UTC grid)


def _k(t: int, o: float, h: float, l: float, c: float, v: float, tb: float) -> list:
    return [t * 1000, str(o), str(h), str(l), str(c), str(v), t * 1000 + 59_999, "0", 1, str(tb), "0", "0"]


def _fresh_cache(monkeypatch):
    from collections import OrderedDict

    monkeypatch.setattr(crypto, "_pages", OrderedDict())


def test_klines_build_missing_resolutions_on_the_utc_grid_with_taker_buy_flow(monkeypatch):
    _fresh_cache(monkeypatch)
    rows = [_k(T0 + i * 300, 100 + i, 101 + i, 99 + i, 100.5 + i, 10, 6 + i % 2) for i in range(4)]   # four 5m bars

    async def fake(path, params):
        assert path == "klines" and params["interval"] == "5m" and params["symbol"] == "BTCUSDT"
        return [r for r in rows if params["startTime"] <= r[0] <= params["endTime"]]
    monkeypatch.setattr(crypto, "_get", fake)
    got = asyncio.run(crypto.klines("CRYPTO:BTCUSDT", "10", T0, T0 + 3600))   # 10m = two 5m bars each
    assert [r[0] for r in got] == [T0, T0 + 600]
    t, o, h, l, c, v, tb = got[0]
    assert (o, h, l, c, v, tb) == (100.0, 102.0, 99.0, 101.5, 20.0, 13.0)       # open of the first, close of the last


def test_klines_fetch_windows_in_parallel_keep_the_newest_and_cache_closed_ones(monkeypatch):
    _fresh_cache(monkeypatch)
    calls: list[int] = []
    live = {"n": 0, "max": 0}

    async def fake(path, params):
        live["n"] += 1
        live["max"] = max(live["max"], live["n"])
        await asyncio.sleep(0.01)                  # overlapping calls = fetched in parallel
        live["n"] -= 1
        calls.append(params["startTime"] // 1000)
        w0 = params["startTime"] // 1000
        return [_k(w0 + i * 60, 1, 1, 1, 1, 1, 0.5) for i in range(crypto.PAGE)]
    monkeypatch.setattr(crypto, "_get", fake)
    monkeypatch.setattr(crypto, "MAX_PAGES", 3)
    win = 60 * crypto.PAGE
    to = T0 - 10 * 86400                           # long closed: every window is history
    got = asyncio.run(crypto.klines("CRYPTO:ETHUSDT", "1", to - 30 * 86400, to))
    assert len(calls) == 3 and live["max"] > 1                         # newest three windows, concurrently
    assert max(calls) == to - to % win and got[-1][0] <= to            # ends at the newest candle asked for
    assert [r[0] for r in got] == sorted(r[0] for r in got)            # oldest first, contiguous
    calls.clear()
    asyncio.run(crypto.klines("CRYPTO:ETHUSDT", "1", to - 30 * 86400, to))
    assert calls == []                                                 # closed windows came from the cache


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


def test_mcx_and_currency_trade_on_paper_only_in_whole_quoted_unit_lots(client, monkeypatch):
    from app.api.orders import market_problem
    from app.services.instrument_master import Instrument, get_master

    m = get_master()
    for sym, short, seg, lot in (("MCX:GOLD26DECFUT", "GOLD", "COM", 100), ("NSE:USDINR26OCTFUT", "USDINR", "CD", 1000),
                                 ("MCX:NICKEL26OCTFUT", "NICKEL", "COM", 0)):
        m._add(Instrument(symbol=sym, short_name=short, exchange=sym.split(":")[0], segment=seg, instrument_type="FUT",
                          lot_size=lot, tick_size=1.0))
    assert market_problem("NSE:SBIN-EQ", 7, paper=False) is None                    # Indian cash / F&O: untouched
    assert "Paper account only" in market_problem("MCX:GOLD26DECFUT", 100, paper=False)
    assert "Paper account only" in market_problem("CRYPTO:BTCUSDT", 1, paper=False)
    assert market_problem("MCX:GOLD26DECFUT", 200, paper=True) is None              # 2 lots of 1 kg
    assert "multiple of 100" in market_problem("MCX:GOLD26DECFUT", 150, paper=True)
    assert "multiple of 1000" in market_problem("NSE:USDINR26OCTFUT", 1, paper=True)
    assert "contract size" in market_problem("MCX:NICKEL26OCTFUT", 1, paper=True)  # never guessed
    r = client.post("/api/orders", json={"account_id": 1, "symbol": "CRYPTO:BTCUSDT", "side": "BUY", "quantity": 1,
                                         "order_type": "MARKET"})
    assert r.status_code == 422 and "Paper account only" in r.json()["detail"]


def test_master_counts_mcx_and_currency_lots_in_the_quoted_unit(tmp_path):
    def row(sym, short, seg):
        return ",".join(["1", short, "30", "1", "1.0", "", "0900-2330", "2026-10-17", "1796407200", sym, "11", seg,
                         "1", short, "114", "-1.0", "XX", "1", "1796407200", "0", "0.0"])
    rows = [row("MCX:GOLD26DECFUT", "GOLD", "20"), row("MCX:SILVERM26NOVFUT", "SILVERM", "20"),
            row("NSE:USDINR26OCTFUT", "USDINR", "12"), row("MCX:COTTON26NOVFUT", "COTTON", "20")]
    (tmp_path / "X.csv").write_text("".join(r + chr(10) for r in rows), encoding="utf-8")
    m = InstrumentMaster(data_dir=tmp_path)
    lots = [m.get(s).lot_size for s in ("MCX:GOLD26DECFUT", "MCX:SILVERM26NOVFUT", "NSE:USDINR26OCTFUT", "MCX:COTTON26NOVFUT")]
    assert lots == [100, 5, 1000, 0]                                        # Fyers says 1 for each
    assert "1 qty = 0.001 BTC" in m.get("CRYPTO:BTCUSDT").to_dict()["display"]


def test_paper_crypto_round_trip_books_rupees(client, monkeypatch, isolated_db):
    from app.api import practice
    from app.db.models import Trade

    practice._backend = None
    _fresh_cache(monkeypatch)
    monkeypatch.setitem(crypto.USD_INR, "rate", 90.0)
    px = {"last": 80000.0, "bid": 79999.0, "ask": 80001.0}

    async def fake(path, params):
        return [{"symbol": "BTCUSDT", "lastPrice": str(px["last"]), "priceChange": "0", "priceChangePercent": "0",
                 "bidPrice": str(px["bid"]), "askPrice": str(px["ask"])}]
    monkeypatch.setattr(crypto, "_get", fake)
    buy = client.post("/api/orders", json={"account_id": -1, "symbol": "CRYPTO:BTCUSDT", "side": "BUY", "quantity": 2,
                                           "order_type": "MARKET"}).json()
    assert buy["ok"] and buy["status"] == "FILLED"
    px.update(last=81000.0, bid=80999.0, ask=81001.0)
    client.app.state.execution_manager.market_data.set_quote_sync(       # the live stream's next tick
        "CRYPTO:BTCUSDT", 81000.0, bid=80999.0, ask=81001.0, extra={"source": "binance"})
    sell = client.post("/api/orders", json={"account_id": -1, "symbol": "CRYPTO:BTCUSDT", "side": "SELL", "quantity": 2,
                                            "order_type": "MARKET"}).json()
    assert sell["ok"] and sell["status"] == "FILLED"
    with practice.session_factory()() as db:
        pnl = db.query(Trade).filter(Trade.symbol == "CRYPTO:BTCUSDT", Trade.side == "SELL").one().pnl
    assert pnl == pytest.approx((80999 - 80001) * 2 * 0.001 * 90.0)     # $998 x 0.002 BTC x 90 = Rs 179.64
    assert crypto.point_value("NSE:SBIN-EQ") == 1.0
    assert client.get("/api/market/fx").json()["units"]["BTCUSDT"] == 0.001
    from app.db.models import Position

    with practice.session_factory()() as db:                            # the practice DB is shared by the suite
        db.query(Trade).filter(Trade.symbol == "CRYPTO:BTCUSDT").delete()
        db.query(Position).filter(Position.symbol == "CRYPTO:BTCUSDT").delete()
        db.commit()
    practice._watcher.cancel()
    practice._backend = None


def test_history_quotes_and_flow_route_crypto_to_binance(client, monkeypatch):
    _fresh_cache(monkeypatch)

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
