"""Trade page symbol search and option chain: master reloads, symbol
resolution, and the live/static expiry hand-off. No Fyers endpoint is
called: the live path runs on stub backends and a mock transport."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app.api import options as options_api
from app.execution.fyers_live import FyersAPIError, FyersClient, FyersLiveBackend
from app.services.instrument_master import InstrumentMaster

IST = timezone(timedelta(hours=5, minutes=30))


def _epoch(y: int, m: int, d: int) -> str:
    return str(int(datetime(y, m, d, 15, 30, tzinfo=IST).timestamp()))


# -- instrument master --------------------------------------------------------


def test_csv_relisting_a_seed_scrip_replaces_it_in_search(tmp_path) -> None:
    (tmp_path / "NSE_CM.csv").write_text("NSE:SBIN-EQ,SBIN,NSE,EQ,EQ,1,0.01\n")
    m = InstrumentMaster(data_dir=tmp_path)
    hits = [h for h in m.search("SBIN") if h.symbol == "NSE:SBIN-EQ"]
    assert len(hits) == 1
    assert hits[0].tick_size == 0.01
    assert m.get("NSE:SBIN-EQ").tick_size == 0.01


def test_reload_replaces_options_under_their_underlying(tmp_path) -> None:
    row = "NSE:NIFTY26OCT25000CE,NIFTY,NSE,FO,CE,{lot},0.05,27-Oct-2026,25000,NIFTY\n"
    (tmp_path / "a.csv").write_text(row.format(lot=75))
    (tmp_path / "b.csv").write_text(row.format(lot=65))
    m = InstrumentMaster(data_dir=tmp_path)
    strikes = m.option_chain("NIFTY")["strikes"]
    assert len(strikes) == 1
    assert strikes[0]["ce"]["lot_size"] == 65


def test_seed_lists_every_optionable_index() -> None:
    m = InstrumentMaster(data_dir=None)
    for sym in (
        "NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:FINNIFTY-INDEX",
        "NSE:MIDCPNIFTY-INDEX", "NSE:NIFTYNXT50-INDEX", "BSE:SENSEX-INDEX",
        "BSE:BANKEX-INDEX",
    ):
        assert m.get(sym) is not None, sym
        assert m.get(sym).instrument_type == "IND"


# -- symbol resolution --------------------------------------------------------


def test_resolve_symbol_maps_a_stock_short_name_to_its_cash_symbol() -> None:
    assert options_api._resolve_symbol("RELIANCE", None) == "NSE:RELIANCE-EQ"
    assert options_api._resolve_symbol("banknifty", None) == "NSE:NIFTYBANK-INDEX"
    assert options_api._resolve_symbol("ZZZNOPE", None) is None


# -- expiry hand-off between live and static ---------------------------------


def test_master_fallback_keeps_a_live_epoch_expiry(tmp_path, monkeypatch) -> None:
    rows = "".join(
        f"NSE:NIFTY{c}25000CE,NIFTY,NSE,FO,CE,65,0.05,{d},25000,NIFTY\n"
        for c, d in (("26O20", "20-Oct-2026"), ("26O27", "27-Oct-2026"))
    )
    (tmp_path / "fo.csv").write_text(rows)
    m = InstrumentMaster(data_dir=tmp_path)
    monkeypatch.setattr(options_api, "get_master", lambda: m)
    monkeypatch.setattr(options_api, "_ist_today", lambda: "2026-10-09")
    out = options_api._master_chain("NIFTY", _epoch(2026, 10, 27), "test")
    assert out["selected_expiry"] == "2026-10-27"
    assert out["strikes"][0]["ce"]["symbol"] == "NSE:NIFTY26O2725000CE"


def test_master_fallback_skips_lapsed_expiries_and_never_guesses_lot_1(tmp_path, monkeypatch) -> None:
    rows = "".join(
        f"NSE:NIFTY{c}25000CE,NIFTY,NSE,FO,CE,{lot},0.05,{d},25000,NIFTY\n"
        for c, d, lot in (("26O06", "06-Oct-2026", 65), ("26O13", "13-Oct-2026", 1))
    )
    (tmp_path / "fo.csv").write_text(rows)
    m = InstrumentMaster(data_dir=tmp_path)
    monkeypatch.setattr(options_api, "get_master", lambda: m)
    monkeypatch.setattr(options_api, "_ist_today", lambda: "2026-10-09")
    out = options_api._master_chain("NIFTY", None, "test")
    # The 6 Oct expiry has lapsed: the nearest live one is picked, not it.
    assert [e["ts"] for e in out["expiries"]] == ["2026-10-13"]
    assert out["selected_expiry"] == "2026-10-13"
    leg = out["strikes"][0]["ce"]
    assert leg["symbol"] == "NSE:NIFTY26O1325000CE"
    # A static lot of 1 is not a real F&O lot: unknown, so the ticket blocks it.
    assert leg["lot_size"] in (None, 65) and leg["lot_size"] != 1

    # Every expiry lapsed: an empty ladder rather than dead contracts.
    monkeypatch.setattr(options_api, "_ist_today", lambda: "2026-11-01")
    out = options_api._master_chain("NIFTY", None, "test")
    assert out["expiries"] == [] and out["strikes"] == []


class _StubBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_option_chain(self, symbol: str, *, strikecount: int, timestamp: str):
        self.calls.append({"symbol": symbol, "timestamp": timestamp})
        return {
            "symbol": symbol,
            "spot": 25000.0,
            "expiries": [
                {"label": "20-10-2026", "ts": _epoch(2026, 10, 20)},
                {"label": "27-10-2026", "ts": _epoch(2026, 10, 27)},
            ],
            "strikes": [],
        }


class _StubManager:
    def __init__(self, backend: _StubBackend) -> None:
        self._backend = backend

    def _manual_backend_for(self, acc):  # noqa: ANN001
        return self._backend


@pytest.fixture()
def live_chain(monkeypatch):
    backend = _StubBackend()
    monkeypatch.setattr(options_api, "_connected_fyers_account", lambda db: object())
    monkeypatch.setattr(options_api, "_manager", lambda: _StubManager(backend))
    return backend


@pytest.mark.asyncio
async def test_live_chain_ignores_a_static_date_expiry(live_chain) -> None:
    out = await options_api.options_chain(
        underlying="NIFTY", symbol=None, strikecount=5, expiry="2026-10-20", db=None,
    )
    assert live_chain.calls[0]["timestamp"] == ""
    assert out["source"] == "fyers"
    assert out["selected_expiry"] == _epoch(2026, 10, 20)


@pytest.mark.asyncio
async def test_live_chain_passes_a_listed_epoch_through(live_chain) -> None:
    ts = _epoch(2026, 10, 27)
    out = await options_api.options_chain(
        underlying="NIFTY", symbol=None, strikecount=5, expiry=ts, db=None,
    )
    assert live_chain.calls[0]["timestamp"] == ts
    assert out["selected_expiry"] == ts


# -- Fyers error bodies ---------------------------------------------------------


@pytest.mark.asyncio
async def test_option_chain_200_error_body_raises_for_the_fallback() -> None:
    body = {"s": "error", "code": -300, "message": "Invalid symbol"}
    client = FyersClient(
        app_id="APP123", access_token="TOK", max_retries=0,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
    )
    backend = FyersLiveBackend(app_id="APP123", access_token="TOK", broker_account_id=1, client=client)
    try:
        with pytest.raises(FyersAPIError, match="Invalid symbol"):
            await backend.get_option_chain("NSE:ZZZ-EQ")
    finally:
        await client.aclose()


def test_csv_relisting_keeps_the_nse_scrip_ranked_first(tmp_path) -> None:
    # BSE_CM.csv loads before NSE_CM.csv; RELIANCE's NSE line must not drop
    # below its BSE line just because the CSV re-listed the seed scrip.
    (tmp_path / "BSE_CM.csv").write_text("BSE:RELIANCE-EQ,RELIANCE,BSE,EQ,EQ,1,0.05\n")
    (tmp_path / "NSE_CM.csv").write_text("NSE:RELIANCE-EQ,RELIANCE,NSE,EQ,EQ,1,0.10\n")
    m = InstrumentMaster(data_dir=tmp_path)
    hits = m.search("RELIANCE")
    assert hits[0].symbol == "NSE:RELIANCE-EQ"
    assert hits[0].tick_size == 0.10
