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
