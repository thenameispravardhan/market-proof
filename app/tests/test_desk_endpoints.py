"""Market depth (DOM / depth panels) and the server-side UI-state store."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.api import market
from app.execution.fyers_live import FyersLiveBackend


class _DepthClient:
    async def get_depth(self, symbol: str) -> dict:
        return {"s": "ok", "d": {symbol: {
            "totalbuyqty": 1200, "totalsellqty": 800, "ltp": 100.5,
            "upper_ckt": 110.55, "lower_ckt": 90.45,
            "bids": [{"price": 100.4, "volume": 50, "ord": 3}, {"price": 0, "volume": 9, "ord": 1}],
            "ask": [{"price": 100.6, "volume": 70, "ord": 4}],
        }}}


def test_backend_normalises_fyers_depth() -> None:
    be = FyersLiveBackend.__new__(FyersLiveBackend)
    be._client = _DepthClient()  # noqa: SLF001
    book = asyncio.run(be.get_depth("NSE:SBIN-EQ"))
    assert book == {"bids": [[100.4, 50.0, 3.0]], "asks": [[100.6, 70.0, 4.0]],
                    "total_buy": 1200.0, "total_sell": 800.0, "ltp": 100.5,
                    "upper_circuit": 110.55, "lower_circuit": 90.45}


def test_depth_degrades_without_fyers(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(market, "_fyers_backend", lambda: None)
    r = client.get("/api/market/depth", params={"symbol": "NSE:SBIN-EQ"})
    assert r.status_code == 200 and r.json()["ok"] is False


def test_depth_serves_the_book(client: TestClient, monkeypatch) -> None:
    class _B:
        async def get_depth(self, s: str) -> dict:
            return {"bids": [[1.0, 2.0, 1.0]], "asks": [], "total_buy": 2.0, "total_sell": 0.0, "ltp": 1.0}

    monkeypatch.setattr(market, "_fyers_backend", lambda: _B())
    j = client.get("/api/market/depth", params={"symbol": "nse:sbin-eq"}).json()
    assert j["ok"] is True and j["symbol"] == "NSE:SBIN-EQ" and j["bids"] == [[1.0, 2.0, 1.0]]


def test_ui_state_round_trip_and_guards(client: TestClient) -> None:
    assert client.get("/api/settings/ui/trade_layouts").json()["value"] is None
    layouts = [{"id": "L1", "name": "Scalp", "saved": 5, "data": {"chart:prefs": "{}"}}]
    assert client.put("/api/settings/ui/trade_layouts", json={"value": layouts}).json()["ok"] is True
    assert client.get("/api/settings/ui/trade_layouts").json()["value"] == layouts
    assert client.get("/api/settings/ui/Bad Name").status_code == 422
    assert client.put("/api/settings/ui/big", json={"value": "x" * 2_000_001}).status_code == 413
    # a UI row must never surface as a global setting
    assert "ui:trade_layouts" not in client.get("/api/settings").text


def test_fyers_client_forces_ipv4() -> None:
    """Orders must leave from the whitelisted static IPv4 (-50 otherwise)."""
    from app.execution.fyers_live import FyersClient

    async def build():
        c = FyersClient(app_id="X-100", access_token="t")
        http = await c._ensure_client()  # noqa: SLF001
        addr = http._transport._pool._local_address  # noqa: SLF001
        await http.aclose()
        return addr

    assert asyncio.run(build()) == "0.0.0.0"
