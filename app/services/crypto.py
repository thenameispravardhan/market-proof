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
from collections import OrderedDict
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

# Paper contract per 1 qty, in coins (positions are whole numbers): ~$5-100 of each, so small sizes work.
UNIT: dict[str, float] = {
    "BTCUSDT": 0.001, "ETHUSDT": 0.01, "BNBUSDT": 0.01, "SOLUSDT": 0.1, "XRPUSDT": 10, "DOGEUSDT": 100,
    "ADAUSDT": 10, "TRXUSDT": 100, "AVAXUSDT": 1, "LINKUSDT": 1, "DOTUSDT": 1, "LTCUSDT": 0.1, "BCHUSDT": 0.01,
    "SHIBUSDT": 1_000_000, "NEARUSDT": 1, "UNIUSDT": 1, "SUIUSDT": 1, "PEPEUSDT": 1_000_000, "PAXGUSDT": 0.01,
}
# Prices are USDT; P&L is booked in rupees at this rate: the near-month NSE USDINR future, refreshed from Fyers
# every 5 min. ponytail: seeded with its 2026-10-19 price until the first refresh lands.
USD_INR: dict[str, float] = {"rate": 95.05, "at": 0.0}


def point_value(symbol: str) -> float:
    """Rupees per 1-point move per 1 qty: 1 for every Indian instrument (qty is in the quoted unit, MCX and
    currency lots included); a crypto contract's coins x USD/INR."""
    if not is_crypto(symbol):
        return 1.0
    return UNIT.get(pair(symbol), 1.0) * USD_INR["rate"]


async def refresh_usd_inr() -> None:
    from app.api.market import fyers_quotes
    from app.services.instrument_master import get_master

    fut = next((i for i in get_master().search("USDINR", limit=10) if i.instrument_type == "FUT"), None)
    if fut is None:
        return
    q = (await fyers_quotes([fut.symbol])).get(fut.symbol.upper())
    if q is not None and q.last_price and 50 < q.last_price < 200:
        USD_INR.update(rate=float(q.last_price), at=time.time())


# Fyers chart resolution -> (Binance interval, how many of them make one candle). Binance has no 2 / 10 / 20 /
# 45 / 180-minute candles: those are built from smaller ones on the same UTC grid the chart uses for crypto.
INTERVALS: dict[str, tuple[str, int]] = {
    "1": ("1m", 1), "2": ("1m", 2), "3": ("3m", 1), "5": ("5m", 1), "10": ("5m", 2), "15": ("15m", 1),
    "20": ("5m", 4), "30": ("30m", 1), "45": ("15m", 3), "60": ("1h", 1), "120": ("2h", 1), "180": ("1h", 3),
    "240": ("4h", 1), "D": ("1d", 1),
}
PAGE = 1000          # Binance's max klines per call
MAX_PAGES = 20       # 20,000 base candles per request; the chart pages further back on its own
STEP = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}
CACHE_PAGES = 200    # ~120 KB each: closed pages are history, they never change
_pages: "OrderedDict[tuple[str, str, int], list[tuple]]" = OrderedDict()
_client: httpx.AsyncClient | None = None


def is_crypto(symbol: str | None) -> bool:
    return (symbol or "").strip().upper().startswith(PREFIX)


def pair(symbol: str) -> str:
    return symbol.strip().upper().split(":", 1)[1]


async def _get(path: str, params: dict[str, Any]) -> Any:
    global _client
    if _client is None or _client.is_closed:          # one kept-alive connection: no TLS handshake per page
        _client = httpx.AsyncClient(timeout=15.0, limits=httpx.Limits(max_connections=8))
    r = await _client.get(f"{REST}/{path}", params=params)
    r.raise_for_status()
    return r.json()


async def _page(p: str, iv: str, w0: int) -> list[tuple]:
    """One aligned window of PAGE candles from w0 (epoch s): (t, o, h, l, c, v, taker_buy). Cached once closed."""
    key = (p, iv, w0)
    if key in _pages:
        _pages.move_to_end(key)
        return _pages[key]
    span = STEP[iv] * PAGE
    rows = await _get("klines", {"symbol": p, "interval": iv, "startTime": w0 * 1000,
                                 "endTime": (w0 + span) * 1000 - 1, "limit": PAGE})
    out = [(int(r[0]) // 1000, *(float(r[i]) for i in (1, 2, 3, 4, 5, 9))) for r in rows]
    if w0 + span < time.time() - STEP[iv]:             # every candle in it has closed
        _pages[key] = out
        while len(_pages) > CACHE_PAGES:
            _pages.popitem(last=False)
    return out


async def klines(symbol: str, res: str, from_ts: int, to_ts: int) -> list[list[float]]:
    """[t, open, high, low, close, volume, taker_buy_volume] per candle in [from_ts, to_ts] (epoch s), oldest first.
    Fetched as aligned PAGE-candle windows in parallel; a range over MAX_PAGES drops its oldest end, never the newest."""
    if res not in INTERVALS:
        raise ValueError(f"{res} candles aren't available for crypto: pick minutes (1-240) or D")
    if pair(symbol) not in PAIRS:
        raise ValueError(f"{symbol} is not a listed crypto pair")
    iv, k = INTERVALS[res]
    win = STEP[iv] * PAGE
    starts = list(range(from_ts - from_ts % win, to_ts + 1, win))[-MAX_PAGES:]   # aligned windows, newest kept
    sem = asyncio.Semaphore(8)

    async def one(w0: int) -> list[tuple]:
        async with sem:
            return await _page(pair(symbol), iv, w0)
    pages = await asyncio.gather(*(one(w) for w in starts))          # in parallel, not page after page
    span = 86400 if res == "D" else int(res) * 60
    out: list[list[float]] = []
    for t, o, h, l, c, v, tb in (x for p in pages for x in p):
        if t < from_ts or t > to_ts:
            continue
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
    """{symbol: {ltp, change, change_pct, bid, ask}} for the listed pairs among `symbols`; {} on a Binance failure."""
    want = [s for s in symbols if is_crypto(s) and pair(s) in PAIRS]
    if not want:
        return {}
    try:
        rows = await _get("ticker/24hr", {"symbols": json.dumps([pair(s) for s in want], separators=(",", ":"))})
    except Exception as e:  # noqa: BLE001
        log.warning("crypto.quotes_failed", error=str(e)[:200])
        return {}
    return {PREFIX + r["symbol"]: {"ltp": float(r["lastPrice"]), "change": float(r["priceChange"]),
                                    "change_pct": float(r["priceChangePercent"]),
                                    "bid": float(r["bidPrice"]) if r.get("bidPrice") else None,
                                    "ask": float(r["askPrice"]) if r.get("askPrice") else None} for r in rows}


class Feed:
    """One Binance stream for every crypto pair a browser is showing, published on the "quotes" channel in the
    same shape as the Fyers ticks, so the chart, watchlist and tape take it unchanged. volume = quantity traded
    since this connection opened (monotonic: the chart adds its rises to the live candle)."""

    THROTTLE_S = 0.25        # ponytail: at most 4 pushes a second per pair; a refresh redraws the exact candle

    def __init__(self) -> None:
        self.cum: dict[str, float] = {}
        self.tk: dict[str, dict[str, float]] = {}
        self.last_pub: dict[str, float] = {}

    async def run(self, watched: Callable[[], list[str]], market_data: Any) -> None:
        """Ticks go into the shared market-data cache like Fyers ticks: the chart (via its /ws relay), paper fills,
        resting paper orders and the paper SL / target watcher all read them there."""
        import websockets

        fx_at = [0.0]

        async def publish(p: dict[str, Any]) -> None:
            await market_data.publish(p["symbol"], p["last_price"], bid=p["bid"], ask=p["ask"], volume=p["volume"],
                                      change=p["change"], change_pct=p["change_pct"], prev_close=p["prev_close"],
                                      extra={"source": "binance"})
            if time.monotonic() - fx_at[0] > 300:      # crypto on screen: keep the rupee rate fresh
                fx_at[0] = time.monotonic()
                asyncio.get_running_loop().create_task(refresh_usd_inr())

        def want_now() -> frozenset[str]:
            return frozenset(pair(s).lower() for s in watched() if is_crypto(s) and pair(s) in PAIRS)

        streams = lambda ps: [f"{p}@{s}" for p in sorted(ps) for s in ("aggTrade", "ticker")]  # noqa: E731
        while True:
            want = want_now()
            if not want:
                await asyncio.sleep(0.5)
                continue
            try:
                async with websockets.connect(WS + "/".join(streams(want)), open_timeout=10, ping_interval=20) as ws:
                    self.cum, n, checked = {}, 0, time.monotonic()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 0.5)
                        except asyncio.TimeoutError:
                            raw = None
                        if time.monotonic() - checked >= 0.5:      # a pair opened / closed on screen: change the
                            checked, now = time.monotonic(), want_now()   # subscription on this socket, no reconnect
                            for method, ps in (("SUBSCRIBE", now - want), ("UNSUBSCRIBE", want - now)):
                                if ps:
                                    n += 1
                                    await ws.send(json.dumps({"method": method, "params": streams(ps), "id": n}))
                            want = now
                            if not want:
                                break
                        if raw:
                            payload = self.on_message(json.loads(raw))
                            if payload and payload["last_price"] is not None:
                                await publish(payload)
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
