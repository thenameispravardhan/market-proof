"""Tests for GET /api/market/history (Trade-page chart candles).

The endpoint keeps the market API's "always 200" contract: broker
problems and bad input degrade to `{ok: false, reason}` so the chart
renders an inline message instead of an error page.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import market


CANDLES = [
    [1750000000, 100.0, 101.5, 99.5, 101.0, 12000],
    [1750000300, 101.0, 102.0, 100.5, 101.5, 9000],
]


class _StubBackend:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def get_history_range(
        self, symbol: str, *, resolution: str, from_ts: int, to_ts: int
    ) -> list:
        self.calls.append(
            {
                "symbol": symbol,
                "resolution": resolution,
                "from_ts": from_ts,
                "to_ts": to_ts,
            }
        )
        return CANDLES


def test_history_degrades_without_fyers(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(market, "_fyers_backend", lambda: None)
    r = client.get(
        "/api/market/history",
        params={"symbol": "NSE:SBIN-EQ", "resolution": "5", "from": 1, "to": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["candles"] == []
    assert "Fyers" in body["reason"]


def test_history_passes_through_candles(client: TestClient, monkeypatch) -> None:
    stub = _StubBackend()
    monkeypatch.setattr(market, "_fyers_backend", lambda: stub)
    r = client.get(
        "/api/market/history",
        params={
            "symbol": "nse:sbin-eq",  # lower-cased on purpose
            "resolution": "15",
            "from": 1_749_990_000,
            "to": 1_750_000_600,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["candles"] == CANDLES
    assert body["symbol"] == "NSE:SBIN-EQ"  # upper-cased before the broker call
    assert stub.calls == [
        {
            "symbol": "NSE:SBIN-EQ",
            "resolution": "15",
            "from_ts": 1_749_990_000,
            "to_ts": 1_750_000_600,
        }
    ]


def test_history_broker_failure_is_not_empty_data(client: TestClient, monkeypatch) -> None:
    """A failed broker call (None) must surface as ok:false so the chart
    offers a retry — NOT as ok:true with an empty candle list."""

    class _FailingBackend:
        async def get_history_range(self, *a, **k):  # noqa: ANN002, ANN003
            return None

    monkeypatch.setattr(market, "_fyers_backend", lambda: _FailingBackend())
    r = client.get(
        "/api/market/history",
        params={"symbol": "NSE:SBIN-EQ", "resolution": "5", "from": 1, "to": 2},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is False
    assert body["candles"] == []
    assert "retry" in body["reason"].lower()


def test_history_rejects_bad_resolution(client: TestClient, monkeypatch) -> None:
    stub = _StubBackend()
    monkeypatch.setattr(market, "_fyers_backend", lambda: stub)
    r = client.get(
        "/api/market/history",
        params={"symbol": "NSE:SBIN-EQ", "resolution": "7", "from": 1, "to": 2},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is False
    assert "resolution" in body["reason"]
    assert stub.calls == []  # rejected before reaching the broker


def test_history_rejects_empty_range(client: TestClient, monkeypatch) -> None:
    stub = _StubBackend()
    monkeypatch.setattr(market, "_fyers_backend", lambda: stub)
    r = client.get(
        "/api/market/history",
        params={"symbol": "NSE:SBIN-EQ", "resolution": "5", "from": 100, "to": 100},
    )
    body = r.json()
    assert body["ok"] is False
    assert stub.calls == []


def test_history_accepts_second_resolutions(client: TestClient, monkeypatch) -> None:
    """The chart's 5s … 45s intervals pass straight through to Fyers."""
    stub = _StubBackend()
    monkeypatch.setattr(market, "_fyers_backend", lambda: stub)
    r = client.get(
        "/api/market/history",
        params={"symbol": "NSE:SBIN-EQ", "resolution": "15s", "from": 1, "to": 2},
    )
    body = r.json()
    assert body["ok"] is True
    assert stub.calls[0]["resolution"] == "15S"


def test_history_asks_for_open_interest_only_when_requested(client: TestClient, monkeypatch) -> None:
    """`oi=1` (the chart's OI indicators on a derivative) reaches the broker
    as `oi=True`; plain requests keep the old call shape."""
    seen: list[dict] = []

    class _OiBackend:
        async def get_history_range(self, symbol: str, **kw):  # noqa: ANN003
            seen.append(kw)
            return [[1750000000, 1.0, 2.0, 0.5, 1.5, 10, 4500]]

    monkeypatch.setattr(market, "_fyers_backend", lambda: _OiBackend())
    base = {"symbol": "NSE:NIFTY26OCTFUT", "resolution": "5", "from": 1, "to": 2}
    body = client.get("/api/market/history", params={**base, "oi": 1}).json()
    assert body["ok"] is True and body["candles"][0][6] == 4500
    client.get("/api/market/history", params=base)
    assert seen[0].get("oi") is True
    assert "oi" not in seen[1]


def test_fyers_client_sends_oi_flag(monkeypatch) -> None:
    """The REST call carries oi_flag=1 only when asked."""
    import asyncio

    from app.execution.fyers_live import FyersClient

    client = FyersClient(app_id="APP-100", access_token="tok")
    calls: list[dict] = []

    async def fake_request(method, path, params=None, base_url=None, **kw):  # noqa: ANN001, ANN003
        calls.append(params or {})
        return {"candles": []}

    monkeypatch.setattr(client, "_request", fake_request)
    asyncio.run(client.get_history("NSE:X", resolution="5", range_from="1", range_to="2", date_format=0, oi_flag=1))
    asyncio.run(client.get_history("NSE:X", resolution="5", range_from="1", range_to="2", date_format=0))
    assert calls[0]["oi_flag"] == "1"
    assert "oi_flag" not in calls[1]


def test_funds_degrades_without_fyers(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(market, "_fyers_backend", lambda: None)
    body = client.get("/api/market/funds").json()
    assert body == {"ok": False, "available": None, "reason": "connect a Fyers account for funds"}


def test_funds_reports_the_available_balance(client: TestClient, monkeypatch) -> None:
    class _Funds:
        async def get_funds(self) -> float:
            return 125000.5

    monkeypatch.setattr(market, "_fyers_backend", lambda: _Funds())
    body = client.get("/api/market/funds").json()
    assert body["ok"] is True
    assert body["available"] == 125000.5


def test_funds_broker_failure(client: TestClient, monkeypatch) -> None:
    class _Broken:
        async def get_funds(self) -> float:
            raise RuntimeError("boom")

    monkeypatch.setattr(market, "_fyers_backend", lambda: _Broken())
    body = client.get("/api/market/funds").json()
    assert body["ok"] is False
    assert body["available"] is None
