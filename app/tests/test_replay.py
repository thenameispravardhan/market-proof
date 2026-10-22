"""GET /api/replay/contract — futures / options prices for Bar Replay trades.

A replayed week in March 2025 (NIFTY weeklies expired on Thursdays then):
the contracts are long gone from Fyers, so they are priced by the model off
the index's candles; a contract still in the scrip master is priced from its
own real candles instead.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.algo import fno
from app.api import market, replay
from app.services.instrument_master import InstrumentMaster

IST = timezone(timedelta(hours=5, minutes=30))
MON = datetime(2025, 3, 3, 9, 15, tzinfo=IST)


def _bars(start: datetime, days: int, px0: float, drift: float) -> list[list[float]]:
    """5-minute bars, 09:15-15:25, `days` weekdays; close climbs `drift` a bar."""
    out, px = [], px0
    d = start
    while days:
        if d.weekday() < 5:
            t0 = int(d.replace(hour=9, minute=15).timestamp())
            for i in range(75):
                o, c = px, px + drift
                out.append([t0 + i * 300, o, max(o, c) + 1, min(o, c) - 1, c, 1000])
                px = c
            days -= 1
        d += timedelta(days=1)
    return out


INDEX = _bars(MON, 5, 22500.0, 0.5)
VIX = [[r[0], 14.0, 14.2, 13.8, 14.0, 0] for r in INDEX]
LISTED = "NSE:NIFTY2530622500CE"
REAL = [[r[0], 150.0, 152.0, 149.0, 151.0, 500] for r in INDEX[:200]]


class _Stub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_history_range(self, symbol: str, *, resolution: str, from_ts: int, to_ts: int, oi: bool = False):
        self.calls.append((symbol, resolution))
        rows = {"NSE:NIFTY50-INDEX": INDEX if resolution != "D" else [], "NSE:INDIAVIX-INDEX": VIX,
                LISTED: REAL}.get(symbol, [])
        return [r for r in rows if from_ts <= r[0] <= to_ts]


@pytest.fixture()
def stub(monkeypatch) -> _Stub:
    s = _Stub()
    monkeypatch.setattr(market, "_fyers_backend", lambda: s)
    replay._CACHE.clear()
    return s


def _ask(client: TestClient, **kw) -> dict:
    bar = INDEX[30]                                   # Monday 11:45
    params = {"underlying": "NSE:NIFTY50-INDEX", "t": bar[0] + 300, "from": bar[0], "to": INDEX[-1][0] + 300,
              "resolution": "5", **kw}
    r = client.get("/api/replay/contract", params=params)
    assert r.status_code == 200
    return r.json()


def test_an_expired_weekly_call_is_priced_by_the_model_off_the_replayed_index(client, stub):
    body = _ask(client, kind="CE")
    assert body["ok"] is True and body["source"] == "model"
    c = body["contract"]
    spot = INDEX[30][4]
    assert body["spot"] == pytest.approx(spot)
    assert c["strike"] == round(spot / 50) * 50                      # ATM on that day's price
    assert datetime.fromtimestamp(c["expiry"], IST) == datetime(2025, 3, 6, 15, 30, tzinfo=IST)   # that week's Thursday
    assert c["symbol"] is None and c["lot"] == 65 and c["label"] == "NIFTY 06MAR25 22500 CE"
    assert body["iv"] == 14.0 and "India VIX" in body["note"]
    rows = body["candles"]
    assert rows[0][0] == INDEX[30][0]                                 # from the replayed bar on
    assert rows[-1][0] < c["expiry"]                                  # nothing past expiry
    assert body["price_at_t"] == rows[0][4] > 0
    # Black-Scholes at the bar's close: the index close, time to expiry from the close, India VIX as IV.
    want = fno.bs_price(spot, c["strike"], fno.years_to(c["expiry"], INDEX[30][0] + 300), 0.14, "CE")
    assert rows[0][4] == pytest.approx(want, abs=0.05)
    assert body["more"] is False


def test_itm_put_steps_up_the_strikes_and_a_future_carries_cost(client, stub):
    put = _ask(client, kind="PE", strike_mode="ITM", steps=2)
    assert put["contract"]["strike"] == round(INDEX[30][4] / 50) * 50 + 100
    fut = _ask(client, kind="FUT")
    assert fut["ok"] and fut["source"] == "model"
    assert datetime.fromtimestamp(fut["contract"]["expiry"], IST).date() == datetime(2025, 3, 27).date()  # monthly
    assert fut["price_at_t"] > INDEX[30][4]                           # fair value above spot


def test_next_expiry_and_a_continuation_keep_the_contract(client, stub):
    first = _ask(client, kind="CE", expiry="next")
    c = first["contract"]
    assert datetime.fromtimestamp(c["expiry"], IST).date() == datetime(2025, 3, 13).date()
    bar = INDEX[200]
    r = client.get("/api/replay/contract", params={
        "underlying": "NSE:NIFTY50-INDEX", "kind": "CE", "t": bar[0] + 300, "from": bar[0], "to": INDEX[-1][0] + 300,
        "resolution": "5", "strike": c["strike"], "expiry_ts": c["expiry"]}).json()
    assert r["contract"]["strike"] == c["strike"] and r["contract"]["expiry"] == c["expiry"]
    assert r["candles"][0][0] == bar[0]


def test_a_listed_contract_trades_on_its_real_candles(client, stub, monkeypatch):
    exp = int(datetime(2025, 3, 6, 14, 30, tzinfo=IST).timestamp())     # the master's own time of day
    monkeypatch.setattr(InstrumentMaster, "options_of",
                        lambda self, u: [(LISTED, 22500.0, "CE", exp, 75, 0.05),
                                         ("NSE:NIFTY2530622550CE", 22550.0, "CE", exp, 75, 0.05)]
                        if u == "NIFTY" else [])
    body = _ask(client, kind="CE", strike=22500)
    assert body["source"] == "real" and body["contract"]["symbol"] == LISTED
    assert datetime.fromtimestamp(body["contract"]["expiry"], IST).hour == 15        # settles at the close
    assert body["price_at_t"] == 151.0
    assert (LISTED, "5") in stub.calls


def test_what_cannot_be_priced_says_why(client, stub):
    assert "FUT, CE or PE" in _ask(client, kind="XX")["reason"]
    r = client.get("/api/replay/contract", params={"underlying": "NSE:SBIN-EQ", "kind": "CE", "t": 2, "from": 1,
                                                   "to": 5}).json()
    assert r["ok"] is False and "no futures & options" in r["reason"]
    r = client.get("/api/replay/contract", params={"underlying": "NSE:NIFTY25MARFUT", "kind": "CE", "t": 2, "from": 1,
                                                   "to": 5}).json()
    assert r["ok"] is False and "underlying" in r["reason"]


def test_without_fyers_it_degrades(client, monkeypatch):
    monkeypatch.setattr(market, "_fyers_backend", lambda: None)
    replay._CACHE.clear()
    assert "connect a Fyers account" in _ask(client, kind="CE")["reason"]


def test_quotes_on_one_replayed_day_share_the_fetches(client, stub):
    _ask(client, kind="CE")
    n = len(stub.calls)
    bar = INDEX[40]                                    # ten bars on, same day
    client.get("/api/replay/contract", params={"underlying": "NSE:NIFTY50-INDEX", "kind": "CE", "t": bar[0] + 300,
                                               "from": bar[0], "to": INDEX[-1][0] + 300, "resolution": "5"})
    assert len(stub.calls) == n                         # served from the cache
