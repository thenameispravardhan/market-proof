"""Fyers realtime streaming — live prices + order tracking over WebSocket.

Replaces per-symbol REST quote *polling* (which hammered Fyers' 10 req/s
limit → constant 429s) with the Fyers **data socket** for prices, and adds
the **order socket** for real-time fill tracking. Both are outbound
connections, so they need no public URL — unlike the postback webhook.

How it fits:
  - The data socket publishes ticks into the shared `MarketDataBus`
    (`extra={"source": "fyers_ws"}`), the SAME bus + symbol keys the
    `TradeManager`, Trade page and index ticker already read. Nothing
    downstream changes. `QuoteFeed` then treats those fresh ticks as
    authoritative and skips its REST poll (see `QuoteFeed._tick_all`),
    so the 429s disappear while REST stays as a backstop if the socket
    drops.
  - The order socket funnels every order update through the shared
    `reconcile_order_update` — identical behaviour to the postback
    webhook (match by broker_order_id, mirror the fill into positions,
    publish `trades.filled`), and idempotent so the two can't double-apply.

Threading: the `fyers-apiv3` SDK runs each socket on its own background
thread and fires callbacks there. We capture the asyncio loop in `_run`
and bridge every callback back onto it with
`asyncio.run_coroutine_threadsafe`, so all bus writes / DB work happen on
the event loop, never on the SDK thread.

Symbols: the bus is keyed by the bare short name (``THOMASCOOK``) the
managed book uses; the socket needs the full broker id
(``NSE:THOMASCOOK-EQ``). We resolve via `resolve_fyers_symbol` and keep a
reverse map so each tick publishes back under the short key.

Lifecycle mirrors the other services: `start()` launches the background
loop; `stop()` + `wait_until_stopped()` tear it down. The loop lazily
(re)connects when a live Fyers account appears and reconnects when its
access token rotates (detected by re-reading `backend.ws_access_token`,
which the execution manager rebuilds on `broker.token_rotated`).
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.execution.base import safe_float
from app.execution.market_data import MarketDataBus
from app.execution.order_reconcile import reconcile_order_update
from app.execution.symbols import resolve_fyers_symbol
from app.logging_config import get_logger

log = get_logger(__name__)


BackendProvider = Callable[[], Optional[Any]]


# ---- default factories (real SDK; tests inject fakes) -------------------


def _default_backend_provider() -> Optional[Any]:
    """The live backend for the connected real Fyers account, or None."""
    from app.api.market import _fyers_backend

    return _fyers_backend()


def _sdk_log_dir() -> str:
    """Keep the SDK's chatty socket logs out of the repo root — write them
    into ``logs/`` (already git-ignored). Returns a path the SDK appends
    its own filename to."""
    path = os.path.join(os.getcwd(), "logs")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return os.getcwd()
    return path


def _default_data_socket_factory(access_token: str, **callbacks: Any) -> Any:
    from fyers_apiv3.FyersWebsocket import data_ws

    return data_ws.FyersDataSocket(
        access_token=access_token,
        write_to_file=False,
        log_path=_sdk_log_dir(),
        litemode=False,  # full mode → ltp + bid/ask + day change
        reconnect=True,
        on_message=callbacks.get("on_message"),
        on_connect=callbacks.get("on_connect"),
        on_error=callbacks.get("on_error"),
        on_close=callbacks.get("on_close"),
    )


def _default_order_socket_factory(access_token: str, **callbacks: Any) -> Any:
    from fyers_apiv3.FyersWebsocket import order_ws

    return order_ws.FyersOrderSocket(
        access_token=access_token,
        write_to_file=False,
        log_path=_sdk_log_dir(),
        on_orders=callbacks.get("on_orders"),
        on_connect=callbacks.get("on_connect"),
        on_error=callbacks.get("on_error"),
        on_close=callbacks.get("on_close"),
        reconnect=True,
    )


def _default_session_factory() -> Callable[[], Any]:
    from app.db.session import SessionLocal

    return SessionLocal


def _extract_order(message: Any) -> dict[str, Any]:
    """The order socket delivers ``{"s": "ok", "orders": {<order>}}``.
    `_unwrap_fyers` only unwraps a *list* `orders`, so pull the inner dict
    out here before reconciliation."""
    if isinstance(message, dict):
        inner = message.get("orders")
        if isinstance(inner, dict):
            return inner
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            return inner[0]
        return message
    return {}


class FyersStreamManager:
    """Owns the Fyers data + order WebSockets and bridges them to asyncio."""

    def __init__(
        self,
        *,
        market_data: MarketDataBus,
        quote_feed: Optional[Any] = None,
        backend_provider: Optional[BackendProvider] = None,
        data_socket_factory: Optional[Callable[..., Any]] = None,
        order_socket_factory: Optional[Callable[..., Any]] = None,
        reconcile_fn: Optional[Callable[..., Any]] = None,
        session_factory: Optional[Callable[[], Any]] = None,
        resolve_fn: Optional[Callable[[str], Optional[str]]] = None,
        always_subscribe: Optional[list[str]] = None,
        reconcile_interval_s: float = 2.0,
        fresh_window_s: float = 6.0,
        ondemand_ttl_s: float = 12.0,
        silence_watchdog_s: float = 30.0,
    ) -> None:
        self._md = market_data
        self._quote_feed = quote_feed
        self._backend_provider = backend_provider or _default_backend_provider
        self._data_factory = data_socket_factory or _default_data_socket_factory
        self._order_factory = order_socket_factory or _default_order_socket_factory
        self._reconcile_fn = reconcile_fn or reconcile_order_update
        self._session_factory = session_factory
        self._resolve = resolve_fn or resolve_fyers_symbol
        # Full Fyers ids kept subscribed for the whole session regardless of
        # the position book — the status-bar index ticker (NIFTY / SENSEX /
        # BANKNIFTY) so it streams sub-second like everything else. Their bus
        # key IS the full id, so the dashboard matches on the index symbol.
        self._always_subscribe = [s.upper() for s in (always_subscribe or [])]
        self._interval = float(reconcile_interval_s)
        # A bus tick counts as "live" for this long; an on-demand
        # subscription is protected from the reconcile sweep for this long
        # so it isn't yanked before / just after serving its first price.
        self._fresh_window = float(fresh_window_s)
        self._ondemand_ttl = float(ondemand_ttl_s)
        # If we're connected + subscribed and haven't seen a tick in this
        # long, emit a `fyers_stream.silent` warning. Off-market this will
        # fire too — the operator filters. During market hours a 30s gap
        # across multiple subscribed symbols means something is wrong.
        self._silence_watchdog_s = float(silence_watchdog_s)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._data_socket: Optional[Any] = None
        self._order_socket: Optional[Any] = None
        self._data_connected = False
        self._token: Optional[str] = None
        self._subscribed: dict[str, str] = {}    # short -> full
        self._full_to_short: dict[str, str] = {}  # full  -> short
        self._ondemand: dict[str, float] = {}     # short -> monotonic ts
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        # Diagnostics: how many ticks we received vs published vs dropped,
        # surfaced periodically so we can tell "socket silent" from "ticks
        # arriving but not mapping" without guessing.
        self._rx_count = 0
        self._pub_count = 0
        self._drop_count = 0
        self._logged_first_msg = False
        self._last_stats_log = 0.0
        # Monotonic ts of the last received data-frame message. Used by
        # the silence watchdog — None until the first tick arrives so we
        # don't fire the watchdog during the startup window before the
        # market opens.
        self._last_rx_ts: Optional[float] = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="fyers-stream")
        return self._task

    def stop(self) -> None:
        self._stop_event.set()

    async def wait_until_stopped(self) -> None:
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    # -- main loop -------------------------------------------------------

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        log.info("fyers_stream.loop_start")
        try:
            while not self._stop_event.is_set():
                try:
                    await self._ensure_connected()
                    if self._data_connected:
                        await self._reconcile_subscriptions()
                        self._log_stats()
                except Exception:  # noqa: BLE001
                    log.exception("fyers_stream.iteration_failed")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                    break
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._close_sockets()
            log.info("fyers_stream.stop")

    def _log_stats(self) -> None:
        """Periodic heartbeat so it's obvious whether ticks are flowing:
        received=0 → socket silent / feed off; received>0 but published=0 →
        ticks arriving but not mapping (symbol-format bug). Also fires the
        silence watchdog when we're connected + subscribed but no ticks
        have arrived in `silence_watchdog_s` seconds."""
        now = time.monotonic()
        if now - self._last_stats_log < 5.0:
            return
        self._last_stats_log = now
        silent = (
            self._data_connected
            and bool(self._subscribed)
            and self._last_rx_ts is not None
            and (now - self._last_rx_ts) > self._silence_watchdog_s
        )
        log.info(
            "fyers_stream.rx_stats",
            received=self._rx_count,
            published=self._pub_count,
            dropped=self._drop_count,
            subscribed=len(self._subscribed),
            silent=silent,
        )
        if silent:
            log.warning(
                "fyers_stream.silent",
                threshold_s=self._silence_watchdog_s,
                last_rx_age_s=round(now - (self._last_rx_ts or now), 1),
                subscribed=len(self._subscribed),
            )

    async def _ensure_connected(self) -> None:
        """Lazily (re)connect: open the sockets once a live account exists,
        reconnect when its access token rotates, and tear down if the
        account disconnects."""
        backend = self._backend_provider()
        if backend is None:
            if self._data_socket is not None:
                log.info("fyers_stream.account_gone_disconnecting")
                await self._close_sockets()
            return
        try:
            token = backend.ws_access_token
        except Exception as e:  # noqa: BLE001
            log.debug("fyers_stream.no_token", error=str(e))
            return
        if self._data_socket is not None and token == self._token:
            return  # already connected with the current token
        if self._data_socket is not None:
            log.info("fyers_stream.token_rotated_reconnecting")
            await self._close_sockets()
        self._token = token
        await self._connect_sockets()

    async def _connect_sockets(self) -> None:
        self._subscribed.clear()
        self._full_to_short.clear()
        self._ondemand.clear()
        self._data_connected = False
        self._data_socket = self._data_factory(
            self._token,
            on_message=self._on_data_message,
            on_connect=self._on_data_connect,
            on_error=self._on_data_error,
            on_close=self._on_data_close,
        )
        self._order_socket = self._order_factory(
            self._token,
            on_orders=self._on_order_update,
            on_connect=self._on_order_connect,
            on_error=self._on_order_error,
            on_close=self._on_order_close,
        )
        # connect() spawns the SDK's ws/ping threads but does a synchronous
        # time.sleep(2), so run it off the event loop.
        await self._loop.run_in_executor(None, self._data_socket.connect)
        await self._loop.run_in_executor(None, self._order_socket.connect)
        self._data_connected = True
        # Order socket subscribes itself in `_on_order_connect` — that
        # covers both the initial connect AND any SDK auto-reconnect
        # (which wipes the order subscription the same way it wipes
        # data subscriptions). No explicit subscribe needed here.
        log.info("fyers_stream.connected")

    async def _close_sockets(self) -> None:
        for sock in (self._data_socket, self._order_socket):
            if sock is None:
                continue
            try:
                if self._loop is not None:
                    await self._loop.run_in_executor(None, sock.close_connection)
                else:
                    sock.close_connection()
            except Exception:  # noqa: BLE001
                log.debug("fyers_stream.close_failed")
        self._data_socket = None
        self._order_socket = None
        self._data_connected = False
        self._subscribed.clear()
        self._full_to_short.clear()
        self._ondemand.clear()

    # -- subscription reconcile -----------------------------------------

    async def _reconcile_subscriptions(self) -> None:
        """Diff the quote feed's watch set against current subscriptions and
        subscribe / unsubscribe the delta on the data socket."""
        if self._data_socket is None or not self._data_connected:
            return
        watched = (
            set(self._quote_feed.watched_symbols()) if self._quote_feed is not None else set()
        )
        desired: dict[str, str] = {}
        # Always-on subscriptions (index ticker): bus key == full id so the
        # tick publishes back under the symbol the dashboard renders.
        for full in self._always_subscribe:
            desired[full] = full
        for short in watched:
            full = self._resolve(short) or (short if ":" in short else None)
            if full:
                desired[short] = full.upper()

        add = {s: f for s, f in desired.items() if s not in self._subscribed}
        # Don't yank a symbol that was just subscribed on demand to serve a
        # one-shot price (get_live_price) — keep it until its TTL lapses, by
        # which point it's either an open position (in `desired`, kept) or
        # genuinely transient (dropped here).
        now = time.monotonic()
        # Snapshot `_subscribed` before iterating: `_on_data_close` runs
        # on the SDK thread and can clear() it mid-iteration (triggered
        # by the SDK's auto-reconnect wiping server-side subs), which
        # would otherwise raise RuntimeError here on the event loop.
        subscribed_snapshot = dict(self._subscribed)
        remove = {
            s: f
            for s, f in subscribed_snapshot.items()
            if s not in desired and (now - self._ondemand.get(s, 0.0)) > self._ondemand_ttl
        }

        if add:
            try:
                self._data_socket.subscribe(
                    symbols=list(add.values()), data_type="SymbolUpdate"
                )
                for s, f in add.items():
                    self._subscribed[s] = f
                    self._full_to_short[f] = s
                log.info("fyers_stream.subscribed", symbols=list(add.values()))
            except Exception:  # noqa: BLE001
                log.exception("fyers_stream.subscribe_failed")
        if remove:
            try:
                self._data_socket.unsubscribe(
                    symbols=list(remove.values()), data_type="SymbolUpdate"
                )
            except Exception:  # noqa: BLE001
                log.exception("fyers_stream.unsubscribe_failed")
            for s, f in remove.items():
                self._subscribed.pop(s, None)
                self._full_to_short.pop(f, None)
                self._ondemand.pop(s, None)

    # -- WS-first one-shot price (signal / manual pricing) ---------------

    async def get_live_price(
        self, symbol: str, *, timeout: float = 1.5, poll: float = 0.1
    ) -> Optional[float]:
        """Return a live price for `symbol` from the WebSocket, or None.

        WS-first: if the socket is already streaming the symbol, return the
        latest bus tick immediately. Otherwise subscribe on demand and wait
        up to `timeout` seconds for the first tick. Returns None when the
        socket isn't connected or no tick arrives in time — the caller
        (`_real_quote`) then falls back to a one-shot REST quote.

        Transient symbols subscribed here (announcements that never become
        positions) are unsubscribed automatically by the reconcile sweep
        once their on-demand TTL lapses.
        """
        if not symbol:
            return None
        bus_key = symbol.strip().upper()
        full = self._resolve(symbol) or (bus_key if ":" in bus_key else None)
        if not full:
            return None
        full = full.upper()

        # bus-first: already streaming this symbol?
        price = await self._fresh_ws_price(bus_key)
        if price is not None:
            # keep an on-demand subscription alive while there's interest.
            if bus_key in self._ondemand:
                self._ondemand[bus_key] = time.monotonic()
            return price

        # subscribe on demand and wait for the first tick.
        if not self._subscribe_ondemand(bus_key, full):
            return None  # not connected → REST fallback
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(poll)
            waited += poll
            price = await self._fresh_ws_price(bus_key)
            if price is not None:
                return price
        return None  # timed out → REST fallback

    def touch_interest(self, symbol: str) -> None:
        """Mark `symbol` as actively viewed (e.g. the Trade page quote box)
        so it gets subscribed on the data socket and stays subscribed while
        the interest is fresh. Non-blocking; the caller then serves the
        symbol from the bus (which the socket keeps warm). No-op until the
        socket is connected."""
        if not symbol:
            return
        bus_key = symbol.strip().upper()
        full = self._resolve(symbol) or (bus_key if ":" in bus_key else None)
        if full:
            self._subscribe_ondemand(bus_key, full.upper())

    def _subscribe_ondemand(self, bus_key: str, full: str) -> bool:
        """Subscribe `full` on the data socket (idempotent) and refresh its
        keep-alive timestamp. Returns False if the socket isn't connected."""
        if self._data_socket is None or not self._data_connected:
            return False
        self._ondemand[bus_key] = time.monotonic()
        if bus_key not in self._subscribed:
            self._full_to_short[full] = bus_key
            self._subscribed[bus_key] = full
            try:
                self._data_socket.subscribe(symbols=[full], data_type="SymbolUpdate")
            except Exception:  # noqa: BLE001
                log.exception("fyers_stream.ondemand_subscribe_failed", symbol=full)
                return False
        return True

    async def _fresh_ws_price(self, bus_key: str) -> Optional[float]:
        """The latest bus price for `bus_key` iff it's a recent `fyers_ws`
        tick, else None."""
        q = await self._md.get_quote(bus_key)
        if q is None or (q.extra or {}).get("source") != "fyers_ws":
            return None
        age = (datetime.now(timezone.utc) - q.timestamp).total_seconds()
        if age > self._fresh_window:
            return None
        return float(q.last_price) if q.last_price and q.last_price > 0 else None

    # -- data socket callbacks (fire on the SDK thread) ------------------

    # How many unmapped-tick warnings to emit before suppressing — a
    # handful are expected at startup (the 'cn' connect ack and a few
    # echo/subscription-ack frames don't carry a symbol), but a
    # sustained run that drops ticks means the parser is broken and we
    # want loud logs. Capped so a long off-market session doesn't flood
    # the log; the periodic rx_stats keeps the drop counter honest even
    # after suppression kicks in.
    _UNMAPPED_LOG_CAP = 100

    def _on_data_connect(self, *args: Any) -> None:
        self._data_connected = True
        log.info("fyers_stream.data_connected")

    def _on_data_close(self, *args: Any) -> None:
        self._data_connected = False
        # CRITICAL: the Fyers SDK's __on_close wipes its internal
        # `scrips_per_channel` and `symbol_token` before auto-reconnecting
        # (see fyers_apiv3.FyersWebsocket.data_ws.__on_close), so the
        # server-side subscriptions are GONE after a network blip. Clear
        # our tracking too so the next reconcile sweep re-subscribes
        # everything from scratch. `_ondemand` is intentionally NOT cleared
        # — those are time-based TTLs and clearing them would let on-demand
        # subs unsubscribe the instant after reconnect.
        self._subscribed.clear()
        self._full_to_short.clear()
        log.info("fyers_stream.data_closed")

    def _on_data_error(self, *args: Any) -> None:
        log.warning("fyers_stream.data_error", detail=str(args[0]) if args else "")

    def _on_data_message(self, message: Any) -> None:
        try:
            self._rx_count += 1
            self._last_rx_ts = time.monotonic()
            if not self._logged_first_msg:
                self._logged_first_msg = True
                log.info(
                    "fyers_stream.first_data_msg",
                    py_type=type(message).__name__,
                    keys=sorted(message.keys()) if isinstance(message, dict) else None,
                    sample=str(message)[:400],
                )
            if not isinstance(message, dict):
                self._drop_count += 1
                if self._drop_count <= self._UNMAPPED_LOG_CAP:
                    log.warning(
                        "fyers_stream.unmapped_tick",
                        reason="non_dict",
                        py_type=type(message).__name__,
                        drop_count=self._drop_count,
                    )
                return
            short = self._resolve_tick_symbol(message)
            if short is None:
                self._drop_count += 1
                if self._drop_count <= self._UNMAPPED_LOG_CAP:
                    log.warning(
                        "fyers_stream.unmapped_tick",
                        reason="symbol_unresolved",
                        symbol=message.get("symbol"),
                        exchange=message.get("exchange"),
                        known=list(self._subscribed.keys())[:10],
                        drop_count=self._drop_count,
                    )
                elif self._drop_count == self._UNMAPPED_LOG_CAP + 1:
                    log.warning(
                        "fyers_stream.unmapped_tick_suppressed",
                        cap=self._UNMAPPED_LOG_CAP,
                        drop_count=self._drop_count,
                    )
                return
            ltp = safe_float(message.get("ltp") or message.get("last_price"))
            if ltp <= 0:
                self._drop_count += 1
                return
            bid = safe_float(message.get("bid_price") or message.get("bid")) or None
            ask = safe_float(message.get("ask_price") or message.get("ask")) or None
            vol_raw = message.get("vol_traded_today") or message.get("volume")
            volume = int(safe_float(vol_raw)) or None
            ch = message.get("ch")
            chp = message.get("chp")
            pc = message.get("prev_close_price")
            self._publish_threadsafe(
                short,
                ltp,
                bid=bid,
                ask=ask,
                volume=volume,
                change=safe_float(ch) if ch is not None else None,
                change_pct=safe_float(chp) if chp is not None else None,
                prev_close=(safe_float(pc) or None) if pc is not None else None,
            )
            self._pub_count += 1
        except Exception:  # noqa: BLE001
            log.exception("fyers_stream.data_message_failed")

    def _resolve_tick_symbol(self, message: dict[str, Any]) -> Optional[str]:
        """Map an incoming tick to the bus key (short name) it was
        subscribed under — tolerant of the exact symbol format the feed
        echoes, which may differ from the subscription string (with/without
        the ``NSE:`` prefix or the ``-EQ`` suffix)."""
        raw = str(message.get("symbol") or "").upper()
        # 1. exact match on the subscribed full id.
        short = self._full_to_short.get(raw)
        if short is not None:
            return short
        # 2. reconstruct exchange:symbol when the feed splits them.
        exch = str(message.get("exchange") or "").upper()
        if exch and raw and ":" not in raw:
            short = self._full_to_short.get(f"{exch}:{raw}")
            if short is not None:
                return short
        # 3. normalise to a bare short name and match a subscription.
        core = raw.split(":", 1)[1] if ":" in raw else raw
        candidates = {core}
        if "-" in core:
            candidates.add(core.rsplit("-", 1)[0])  # drop -EQ/-BE/-BZ/... suffix
        for sub_short in self._subscribed:
            if sub_short in candidates:
                return sub_short
        return None

    def _publish_threadsafe(self, short: str, ltp: float, **kwargs: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        coro = self._md.publish(
            short, float(ltp), extra={"source": "fyers_ws"}, **kwargs
        )
        asyncio.run_coroutine_threadsafe(coro, loop)

    # -- order socket callbacks (fire on the SDK thread) -----------------

    def _on_order_connect(self, *args: Any) -> None:
        log.info("fyers_stream.order_connected")
        # Same auto-reconnect-loss issue as the data socket: the SDK's
        # __on_close wipes its internal subscription state before
        # reconnecting. The order socket's "OnOrders,OnTrades"
        # subscription is gone after a network blip — re-send it here so
        # we don't miss fill updates. Subscribe is idempotent on the
        # server side, so a duplicate from `_connect_sockets` is fine.
        sock = self._order_socket
        if sock is not None:
            try:
                sock.subscribe("OnOrders,OnTrades")
            except Exception:  # noqa: BLE001
                log.exception("fyers_stream.order_resubscribe_failed")

    def _on_order_close(self, *args: Any) -> None:
        log.info("fyers_stream.order_closed")

    def _on_order_error(self, *args: Any) -> None:
        log.warning("fyers_stream.order_error", detail=str(args[0]) if args else "")

    def _on_order_update(self, message: Any) -> None:
        order = _extract_order(message)
        if not order:
            return
        loop = self._loop
        if loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._reconcile_order(order), loop)

    async def _reconcile_order(self, order: dict[str, Any]) -> None:
        session_factory = self._session_factory or _default_session_factory()
        try:
            with session_factory() as db:
                await self._reconcile_fn(db, order, source="fyers_order_ws")
        except Exception:  # noqa: BLE001
            log.exception("fyers_stream.order_reconcile_failed")
