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
    assert "live MCX orders are off" in market_problem("MCX:GOLD26DECFUT", 100, paper=False)
    assert "Paper account only" in market_problem("CRYPTO:BTCUSDT", 1, paper=False)
    assert "Paper account only" in market_problem("NSE:USDINR26OCTFUT", 1000, paper=False)
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
    syms = {"MCX:GOLD26DECFUT": ("GOLD", "20"), "MCX:SILVERM26NOVFUT": ("SILVERM", "20"),
            "NSE:USDINR26OCTFUT": ("USDINR", "12"), "MCX:COTTON26NOVFUT": ("COTTON", "20"),
            "MCX:NICKEL26OCTFUT": ("NICKEL", "20"), "MCX:SILVER10026OCTFUT": ("SILVER100", "20"),
            "NSE:EURUSD26OCTFUT": ("EURUSD", "12"), "NSE:633GS203526OCTFUT": ("633GS2035", "12")}
    rows = [row(s, short, seg) for s, (short, seg) in syms.items()]
    (tmp_path / "X.csv").write_text("".join(r + chr(10) for r in rows), encoding="utf-8")
    m = InstrumentMaster(data_dir=tmp_path)
    lots = [m.get(s).lot_size for s in syms]
    assert lots == [100, 5, 1000, 25, 250, 10, 1000, 0]                    # Fyers says 1 for each; a bond future: unknown
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


def _contracts():
    """GOLD (lot 100 x 10 g), USDINR (1000 $) and EURUSD (1000 EUR) in the shared master."""
    from app.services.instrument_master import Instrument, get_master

    m = get_master()
    for sym, short, seg, lot, tick in (("MCX:GOLD26DECFUT", "GOLD", "COM", 100, 1.0),
                                       ("NSE:USDINR26OCTFUT", "USDINR", "CD", 1000, 0.0025),
                                       ("NSE:EURUSD26OCTFUT", "EURUSD", "CD", 1000, 0.0001)):
        m._add(Instrument(symbol=sym, short_name=short, exchange=sym.split(":")[0], segment=seg, instrument_type="FUT",
                          lot_size=lot, tick_size=tick))
    return m


def test_ticket_lot_refresh_keeps_mcx_and_currency_lots_in_the_quoted_unit(client, monkeypatch):
    # The Trade page re-reads every saved F&O entry's lot from /api/options/lots: it answered 1 for GOLD (the ticket
    # then sent 1, refused: "1 lot = 100") and null for USDINR (the ticket blocked it as "lot unknown").
    # No scrip-master download: it reloads the master (dropping the contracts planted here) and ties the test to
    # the network and to whatever the live master lists today.
    from app.algo import fno

    monkeypatch.setattr(fno, "master_loaded", lambda: True)
    _contracts()
    lots = client.get("/api/options/lots?symbols=MCX:GOLD26DECFUT,NSE:USDINR26OCTFUT,NSE:SBIN-EQ").json()["lots"]
    assert lots == {"MCX:GOLD26DECFUT": 100, "NSE:USDINR26OCTFUT": 1000, "NSE:SBIN-EQ": 1}


def test_day_orders_expire_at_their_own_session_close():
    from datetime import datetime, timezone

    from app.api.orders import _expired_day_order
    from app.db.models import Trade

    _contracts()
    utc = lambda h, m: datetime(2026, 10, 20, h, m, tzinfo=timezone.utc)   # noqa: E731 — IST = UTC + 5:30
    at_10_ist, at_16_ist = utc(4, 30), utc(10, 30)
    usd = Trade(symbol="NSE:USDINR26OCTFUT", created_at=at_10_ist)
    assert not _expired_day_order(usd, at_16_ist)                 # currency trades to 17:00, not 15:30
    assert _expired_day_order(Trade(symbol="NSE:SBIN-EQ", created_at=at_10_ist), at_16_ist)
    assert _expired_day_order(usd, utc(11, 45))                   # 17:15 IST
    btc = Trade(symbol="CRYPTO:BTCUSDT", created_at=at_10_ist)
    assert not _expired_day_order(btc, at_16_ist)                 # 24/7: no 15:30 close (was "expired" at 15:30)
    assert _expired_day_order(btc, utc(12, 31))                   # the practice engine's 8 h resting life


def test_cross_currency_pairs_trade_on_paper_with_rupee_pnl(monkeypatch):
    from app.algo import fno
    from app.api.orders import market_problem, snap_tick

    _contracts()
    monkeypatch.setitem(crypto.USD_INR, "rate", 90.0)
    monkeypatch.setitem(crypto.JPY_INR, "rate", 0.6)
    assert market_problem("NSE:EURUSD26OCTFUT", 2000, paper=True) is None
    assert fno.lot_error("NSE:EURUSD26OCTFUT", 2000) is None      # was "isn't in the F&O scrip master"
    assert crypto.point_value("NSE:EURUSD26OCTFUT") == 90.0       # $1 per EUR per point, in rupees
    assert crypto.point_value("NSE:USDJPY26OCT164.5CE") == 0.6    # 1 yen per USD per point
    assert crypto.point_value("NSE:USDINR26OCTFUT") == 1.0
    assert snap_tick("CRYPTO:SHIBUSDT", 0.0000123456) == pytest.approx(0.00001235, abs=1e-12)   # was 0.000012


def test_paper_mcx_and_crypto_orders_end_to_end(client, monkeypatch):
    """Practice account, real-price ticks: GOLD in whole 100-unit lots (market + ticket SL / target, limit
    modify / cancel, SL-M trigger, target exit) and a SHIB stop on its 1e-8 tick, P&L in rupees."""
    import functools
    import time as _time

    from app.api import practice
    from app.db.models import Position, Trade

    practice._backend = None
    _contracts()
    monkeypatch.setitem(crypto.USD_INR, "rate", 90.0)
    monkeypatch.setitem(crypto.USD_INR, "at", _time.time())              # fresh: no Fyers refresh attempted
    md = client.app.state.execution_manager.market_data
    gold, shib = "MCX:GOLD26DECFUT", "CRYPTO:SHIBUSDT"

    def tick(sym, px, spread):                     # a live tick: the quote cache + every resting-order subscriber
        client.portal.call(functools.partial(md.publish, sym, px, bid=px - spread, ask=px + spread,
                                             extra={"source": "fyers_ws" if sym == gold else "binance"}))

    def until(cond):
        for _ in range(60):
            if cond():
                return True
            _time.sleep(0.05)
        return False

    def rows(sym):
        with practice.session_factory()() as db:
            pos = db.query(Position).filter(Position.symbol == sym).one_or_none()
            return pos, [(t.side, t.quantity, t.status, t.pnl) for t in db.query(Trade).filter(Trade.symbol == sym).order_by(Trade.id)]

    def order(**kw):
        return client.post("/api/orders", json={"account_id": -1, "symbol": gold, "order_type": "MARKET", **kw})

    try:
        tick(gold, 147000.0, 1.0)
        assert order(side="BUY", quantity=150).status_code == 422                     # a part lot
        r = order(side="BUY", quantity=200, stop_loss=146800, target=147300).json()
        assert r["ok"] and r["status"] == "FILLED"
        p = next(x for x in client.get("/api/positions?practice=1").json() if x["symbol"] == gold)
        assert (p["quantity"], p["average_price"], p["lot_size"], p["stop_loss"], p["target"]) == (200, 147001.0, 100, 146800, 147300)

        lim = order(side="SELL", quantity=100, order_type="LIMIT", limit_price=147500).json()
        assert lim["status"] == "PENDING"
        pend = client.get("/api/orders/pending?account_id=-1").json()["orders"]
        assert [o["quantity"] for o in pend if o["broker_order_id"] == lim["broker_order_id"]] == [100]
        assert client.post("/api/orders/modify", json={"account_id": -1, "broker_order_id": lim["broker_order_id"],
                                                       "limit_price": 147400}).json()["ok"]
        assert client.post("/api/orders/cancel", json={"account_id": -1, "broker_order_id": lim["broker_order_id"]}).json()["ok"]

        slm = order(side="SELL", quantity=100, order_type="SL-M", stop_price=146950).json()
        assert slm["status"] == "PENDING"
        tick(gold, 146940.0, 1.0)                                                     # triggers: sells at the bid
        assert until(lambda: rows(gold)[0].quantity == 100)
        tick(gold, 147310.0, 1.0)                                                     # target: the watcher exits
        assert until(lambda: rows(gold)[0].quantity == 0)
        trades = rows(gold)[1]
        assert ("SELL", 100, "filled", pytest.approx((146939 - 147001) * 100)) in trades
        assert ("SELL", 100, "filled", pytest.approx((147309 - 147001) * 100)) in trades
        assert ("SELL", 100, "cancelled", None) in trades

        tick(shib, 0.00001234, 0.00000001)
        assert client.post("/api/orders", json={"account_id": -1, "symbol": shib, "side": "BUY", "quantity": 3,
                                                 "order_type": "MARKET"}).json()["status"] == "FILLED"
        lv = client.post(f"/api/positions/{shib}/levels?practice=1", json={"stop_loss": 0.000011951, "target": None})
        assert lv.json()["managed"]["stop_loss"] == pytest.approx(0.00001195, abs=1e-14)   # its tick, not 6 dp
        tick(shib, 0.00001190, 0.00000001)
        assert until(lambda: rows(shib)[0].quantity == 0)
        assert rows(shib)[1][-1][3] == pytest.approx((0.00001189 - 0.00001235) * 3 * 1_000_000 * 90.0)
    finally:
        with practice.session_factory()() as db:                      # the practice DB is shared by the suite
            db.query(Trade).filter(Trade.symbol.in_([gold, shib])).delete()
            db.query(Position).filter(Position.symbol.in_([gold, shib])).delete()
            db.commit()
        if practice._watcher is not None:
            practice._watcher.cancel()
        practice._backend = None


@pytest.fixture()
def mcx_live(monkeypatch):
    from app import config as app_config

    monkeypatch.setenv("FYERS_MCX_LIVE", "true")
    app_config.reset_settings_cache()
    _contracts()
    yield
    monkeypatch.delenv("FYERS_MCX_LIVE")
    app_config.reset_settings_cache()


def _fyers(monkeypatch, answer):
    from app.execution.fyers_live import FyersClient, FyersLiveBackend

    client = FyersClient(app_id="APP-100", access_token="TOK")
    calls: list = []

    async def req(method, path, *, params=None, json_body=None, base_url=None):  # noqa: ANN001
        calls.append((method, path, json_body or params))
        return answer(method, path)
    monkeypatch.setattr(client, "_request", req)
    return FyersLiveBackend(app_id="APP-100", access_token="TOK", broker_account_id=1, client=client), calls


def test_algo_lab_live_orders_pass_the_market_gate(monkeypatch):
    from app.algo import runner
    from app.api import orders

    _contracts()
    sent: list = []
    monkeypatch.setattr(orders, "_manager", lambda: sent.append(1))   # never reached for a refused order
    for sym in ("MCX:GOLD26DECFUT", "CRYPTO:BTCUSDT", "NSE:USDINR26OCTFUT"):
        ok, oid, msg = asyncio.run(runner._order(None, sym, "BUY", 1, exit_=False, strategy_id=1))
        assert not ok and oid is None and ("MCX" in msg or "Paper account only" in msg)
    assert sent == []


def test_live_mcx_switch_off_leaves_every_quantity_as_sent(monkeypatch):
    from app.execution.base import OrderSide
    from app.services.instrument_master import fyers_lot

    _contracts()
    assert fyers_lot("MCX:GOLD26DECFUT") == 1
    be, calls = _fyers(monkeypatch, lambda m, p: {"s": "ok", "id": "O1"})
    asyncio.run(be.place_order(signal=None, symbol="MCX:GOLD26DECFUT", side=OrderSide.BUY, quantity=7))
    assert calls[-1][2]["qty"] == 7


def test_live_mcx_sends_fyers_lots_and_reads_them_back_as_units(monkeypatch, mcx_live):
    from app.api.orders import market_problem
    from app.execution.base import OrderSide, OrderState
    from app.services.instrument_master import fyers_lot

    assert (fyers_lot("MCX:GOLD26DECFUT"), fyers_lot("NSE:SBIN-EQ"), fyers_lot("NSE:USDINR26OCTFUT")) == (100, 1, 1)
    assert market_problem("MCX:GOLD26DECFUT", 200, paper=False) is None
    assert "multiple of 100" in market_problem("MCX:GOLD26DECFUT", 150, paper=False)
    assert "Paper account only" in market_problem("NSE:USDINR26OCTFUT", 1000, paper=False)   # currency stays paper

    book = {"id": "O1", "symbol": "MCX:GOLD26DECFUT", "status": 2, "qty": 2, "filledQty": 2, "remainingQuantity": 0,
            "tradedPrice": 147000.0}
    pos = {"symbol": "MCX:GOLD26DECFUT", "netQty": -1, "qty": 1, "buyQty": 0, "sellQty": 1, "netAvg": 147000.0,
           "ltp": 147010.0, "qtyMulti_com": 100, "pl": -1000.0}

    def answer(method, path):
        if path == "/positions" and method == "GET":
            return {"s": "ok", "netPositions": [dict(pos)]}
        if path == "/orders" and method == "GET":
            return {"s": "ok", "orderBook": [dict(book)]}
        return {"s": "ok", "id": "O1", "message": "done"}
    be, calls = _fyers(monkeypatch, answer)

    # place: 200 units = 2 lots; a part lot never reaches Fyers
    r = asyncio.run(be.place_order(signal=None, symbol="MCX:GOLD26DECFUT", side=OrderSide.BUY, quantity=200))
    assert r.state == OrderState.PENDING and calls[-1][2]["qty"] == 2
    n = len(calls)
    r = asyncio.run(be.place_order(signal=None, symbol="MCX:GOLD26DECFUT", side=OrderSide.BUY, quantity=150))
    assert r.state == OrderState.REJECTED and "1 lot = 100" in r.error and len(calls) == n
    # modify: with the symbol, or looked up from the order book when it isn't given
    assert asyncio.run(be.modify_order("O1", quantity=100, symbol="MCX:GOLD26DECFUT"))[0]
    assert calls[-1][2]["qty"] == 1
    assert asyncio.run(be.modify_order("O1", quantity=300))[0]
    assert calls[-2][1] == "/orders" and calls[-1][2]["qty"] == 3
    assert not asyncio.run(be.modify_order("O1", quantity=50, symbol="MCX:GOLD26DECFUT"))[0]
    # position convert
    asyncio.run(be.convert_position("MCX:GOLD26DECFUT", -1, 100, "INTRADAY", "MARGIN"))
    assert calls[-1][2]["convertQty"] == 1
    # reads: positions, order status and the broker book in units
    [p] = asyncio.run(be.get_positions())
    assert p.quantity == -100
    st = asyncio.run(be.get_order_status("O1"))
    assert st.filled_quantity == 200 and st.raw["qty"] == 200
    b = asyncio.run(be.broker_book())
    assert b["orders"][0]["filledQty"] == 200 and b["positions"][0]["netQty"] == -100


def test_live_mcx_fill_lands_in_positions_in_units_once(mcx_live, db_session, isolated_db):
    from app.db.models import Position, Trade
    from app.db.session import SessionLocal
    from app.execution.order_reconcile import fyers_in_units, reconcile_order_update

    with SessionLocal() as s:
        s.add(Trade(symbol="MCX:GOLD26DECFUT", side="BUY", quantity=200, price=147000.0, order_type="LIMIT",
                    status="placed", broker_order_id="MCX-ORD-1"))
        s.commit()
    update = {"id": "MCX-ORD-1", "status": "2", "symbol": "MCX:GOLD26DECFUT", "qty": 2, "filledQty": 2,
              "tradedPrice": 147000.0}
    with SessionLocal() as s:
        assert asyncio.run(reconcile_order_update(s, update, source="fyers_order_ws"))["status"] == "filled"
    with SessionLocal() as s:                                          # the same fill again, already converted
        asyncio.run(reconcile_order_update(s, fyers_in_units(dict(update)), source="fyers_postback"))
        assert s.query(Position).filter_by(symbol="MCX:GOLD26DECFUT").one().quantity == 200
        assert s.query(Trade).filter_by(broker_order_id="MCX-ORD-1").one().filled_qty == 200
