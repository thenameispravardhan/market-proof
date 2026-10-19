"""Crypto spot: candles, quotes, live ticks and true order flow from Binance's public market data.

Watch / chart / analysis only. These symbols are never traded (Fyers can't route them, the order API refuses
them) and never price an Indian instrument: the bot's own pricing stays Fyers-only. data-api / data-stream
.binance.vision are Binance's market-data-only hosts (no key, no account).

Symbols are "CRYPTO:<pair>", e.g. "CRYPTO:BTCUSDT". A candle's buy volume is Binance's taker-buy volume, so
the order flow is exact (each trade's aggressor), not estimated.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from app.logging_config import get_logger

log = get_logger(__name__)

REST = "https://data-api.binance.vision/api/v3"
WS = "wss://data-stream.binance.vision/stream?streams="
PREFIX = "CRYPTO:"

# pair -> (tick size, name). Ticks from Binance exchangeInfo, 2026-10-19.
PAIRS: dict[str, tuple[float, str]] = {
    "BTCUSDT": (0.01, "Bitcoin"), "ETHUSDT": (0.01, "Ethereum"), "BNBUSDT": (0.01, "BNB"),
    "SOLUSDT": (0.01, "Solana"), "XRPUSDT": (0.0001, "XRP"), "DOGEUSDT": (0.00001, "Dogecoin"),
    "ADAUSDT": (0.0001, "Cardano"), "TRXUSDT": (0.0001, "TRON"), "AVAXUSDT": (0.001, "Avalanche"),
    "LINKUSDT": (0.001, "Chainlink"), "DOTUSDT": (0.001, "Polkadot"), "LTCUSDT": (0.01, "Litecoin"),
    "BCHUSDT": (0.1, "Bitcoin Cash"), "SHIBUSDT": (0.00000001, "Shiba Inu"), "NEARUSDT": (0.001, "NEAR"),
    "UNIUSDT": (0.001, "Uniswap"), "SUIUSDT": (0.0001, "Sui"), "PEPEUSDT": (0.00000001, "Pepe"),
    "PAXGUSDT": (0.01, "PAX Gold (1 token = 1 troy oz of gold)"),
}

# Fyers chart resolution -> (Binance interval, how many of them make one candle). Binance has no 2 / 10 / 20 /
# 45 / 180-minute candles: those are built from smaller ones on the same UTC grid the chart uses for crypto.
INTERVALS: dict[str, tuple[str, int]] = {
    "1": ("1m", 1), "2": ("1m", 2), "3": ("3m", 1), "5": ("5m", 1), "10": ("5m", 2), "15": ("15m", 1),
    "20": ("5m", 4), "30": ("30m", 1), "45": ("15m", 3), "60": ("1h", 1), "120": ("2h", 1), "180": ("1h", 3),
    "240": ("4h", 1), "D": ("1d", 1),
}
PAGE = 1000          # Binance's max klines per call
MAX_PAGES = 20       # 20,000 base candles per request; the chart pages further back on its own


def is_crypto(symbol: str | None) -> bool:
    return (symbol or "").strip().upper().startswith(PREFIX)


def pair(symbol: str) -> str:
    return symbol.strip().upper().split(":", 1)[1]


async def _get(path: str, params: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{REST}/{path}", params=params)
        r.raise_for_status()
        return r.json()


async def klines(symbol: str, res: str, from_ts: int, to_ts: int) -> list[list[float]]:
    """[t, open, high, low, close, volume, taker_buy_volume] per candle in [from_ts, to_ts] (epoch s), oldest first.
    Walks back from `to_ts`, so a range over MAX_PAGES drops its oldest end, never the newest."""
    if res not in INTERVALS:
        raise ValueError(f"{res} candles aren't available for crypto: pick minutes (1-240) or D")
    if pair(symbol) not in PAIRS:
        raise ValueError(f"{symbol} is not a listed crypto pair")
    iv, k = INTERVALS[res]
    pages: list[list] = []
    end = to_ts * 1000
    for _ in range(MAX_PAGES):
        page = await _get("klines", {"symbol": pair(symbol), "interval": iv, "endTime": end, "limit": PAGE})
        if not page:
            break
        pages.append(page)
        if page[0][0] <= from_ts * 1000 or len(page) < PAGE:
            break
        end = page[0][0] - 1
    span = 86400 if res == "D" else int(res) * 60
    out: list[list[float]] = []
    for row in (x for p in reversed(pages) for x in p):
        t = int(row[0]) // 1000
        if t < from_ts or t > to_ts:
            continue
        o, h, l, c, v, tb = (float(row[i]) for i in (1, 2, 3, 4, 5, 9))
        b = t - t % span if k > 1 else t
        if out and out[-1][0] == b:                    # a smaller candle of the same bucket
            last = out[-1]
            last[2], last[3], last[4] = max(last[2], h), min(last[3], l), c
            last[5] += v
            last[6] += tb
        else:
            out.append([b, o, h, l, c, v, tb])
    return out


async def quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """{symbol: {ltp, change, change_pct}} for the listed pairs among `symbols`; {} on a Binance failure."""
    want = [s for s in symbols if is_crypto(s) and pair(s) in PAIRS]
    if not want:
        return {}
    try:
        rows = await _get("ticker/24hr", {"symbols": json.dumps([pair(s) for s in want], separators=(",", ":"))})
    except Exception as e:  # noqa: BLE001
        log.warning("crypto.quotes_failed", error=str(e)[:200])
        return {}
    return {PREFIX + r["symbol"]: {"ltp": float(r["lastPrice"]), "change": float(r["priceChange"]),
                                    "change_pct": float(r["priceChangePercent"])} for r in rows}


class Feed:
    """One Binance stream for every crypto pair a browser is showing, published on the "quotes" channel in the
    same shape as the Fyers ticks, so the chart, watchlist and tape take it unchanged. volume = quantity traded
    since this connection opened (monotonic: the chart adds its rises to the live candle)."""

    THROTTLE_S = 0.25        # ponytail: at most 4 pushes a second per pair; a refresh redraws the exact candle

    def __init__(self) -> None:
        self.cum: dict[str, float] = {}
        self.tk: dict[str, dict[str, float]] = {}
        self.last_pub: dict[str, float] = {}

    async def run(self, watched: Callable[[], list[str]]) -> None:
        import websockets

        from app.services.event_bus import event_bus

        def want_now() -> frozenset[str]:
            return frozenset(pair(s).lower() for s in watched() if is_crypto(s) and pair(s) in PAIRS)

        while True:
            want = want_now()
            if not want:
                await asyncio.sleep(2)
                continue
            try:
                url = WS + "/".join(f"{p}@aggTrade/{p}@ticker" for p in sorted(want))
                async with websockets.connect(url, open_timeout=10, ping_interval=20) as ws:
                    self.cum = {}
                    checked = time.monotonic()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 2)
                        except asyncio.TimeoutError:
                            raw = None
                        if time.monotonic() - checked >= 2:
                            checked = time.monotonic()
                            if want_now() != want:
                                break                  # pairs on screen changed: reconnect with the new set
                        if raw:
                            payload = self.on_message(json.loads(raw))
                            if payload:
                                await event_bus.publish("quotes", payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a dropped stream reconnects
                log.warning("crypto.stream_failed", error=str(e)[:200])
                await asyncio.sleep(5)

    def on_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """A quote for the "quotes" channel, or None (throttled / not a price message)."""
        d = msg.get("data") or {}
        s, kind = d.get("s"), d.get("e")
        if not s:
            return None
        now = time.monotonic()
        if kind == "24hrTicker":
            self.tk[s] = {"prev": float(d["x"]), "bid": float(d["b"]), "ask": float(d["a"]), "last": float(d["c"])}
        elif kind == "aggTrade":
            self.cum[s] = self.cum.get(s, 0.0) + float(d["q"])
            self.tk.setdefault(s, {})["last"] = float(d["p"])
            if now - self.last_pub.get(s, 0.0) < self.THROTTLE_S:
                return None
        else:
            return None
        self.last_pub[s] = now
        t = self.tk[s]
        last, prev = t.get("last"), t.get("prev")
        when = datetime.fromtimestamp(float(d.get("T") or d.get("E") or time.time() * 1000) / 1000, timezone.utc)
        return {"symbol": PREFIX + s, "last_price": last, "bid": t.get("bid"), "ask": t.get("ask"),
                "volume": self.cum.get(s, 0.0),
                "change": last - prev if last is not None and prev else None,
                "change_pct": (last - prev) / prev * 100 if last is not None and prev else None,
                "prev_close": prev, "source": "binance", "simulated": False, "ts": when.isoformat()}


feed = Feed()
