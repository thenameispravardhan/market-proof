"""Tests for the realtime Fyers streaming feed.

Covers, with fake SDK sockets (no network), the seams that matter:
  - the data socket subscribes to the resolved full symbol and a tick
    publishes into the bus under the bare short name (source=fyers_ws);
  - an order-socket update funnels through `reconcile_order_update`;
  - `resolve_fyers_symbol` short-name -> full-symbol resolution;
  - the QuoteFeed REST backstop is suppressed only by a fresh `fyers_ws`
    tick, never by its own `fyers` REST publish;
  - `reconcile_order_update` fills a trade + mirrors the position.
"""
from __future__ import annotations

import asyncio

import pytest

from app.execution.fyers_stream import FyersStreamManager
from app.execution.market_data import MarketDataBus
from app.execution.order_reconcile import reconcile_order_update
from app.execution.quote_feed import QuoteFeed
from app.execution.symbols import clear_resolution_cache, resolve_fyers_symbol


# The resolution cache in `app.execution.symbols` is module-level so
# test order can poison it (test A populates `"SBIN"` -> `"NSE:SBIN-EQ"`,
# test B monkeypatches `get_master` and still gets the cached answer).
# Clear it before every test in this module so each test gets a fresh
# resolution.
@pytest.fixture(autouse=True)
def _clear_resolution_cache():
    clear_resolution_cache()
    yield
    clear_resolution_cache()


# ---- fakes --------------------------------------------------------------


class _FakeDataSocket:
    def __init__(self, **cb):
        self._cb = cb
        self.subscribed: list[tuple] = []
        self.unsubscribed: list[tuple] = []
        self.connected = False

    def connect(self):
        self.connected = True
        if self._cb.get("on_connect"):
            self._cb["on_connect"]()

    def subscribe(self, symbols, data_type="SymbolUpdate"):
        self.subscribed.append((tuple(symbols), data_type))

    def unsubscribe(self, symbols, data_type="SymbolUpdate"):
        self.unsubscribed.append((tuple(symbols), data_type))

    def close_connection(self):
        self.connected = False


class _FakeOrderSocket:
    def __init__(self, **cb):
        self._cb = cb
        self.subscribed: list[str] = []

    def connect(self):
        if self._cb.get("on_connect"):
            self._cb["on_connect"]()

    def subscribe(self, data_type):
        self.subscribed.append(data_type)

    def close_connection(self):
        pass


class _FakeBackend:
    ws_access_token = "APP-100:tok"


class _FakeQuoteFeed:
    def __init__(self, symbols):
        self._symbols = list(symbols)

    def watched_symbols(self):
        return list(self._symbols)


def _factories():
    holder: dict = {}

    def data_factory(token, **cb):
        s = _FakeDataSocket(**cb)
        holder["data"] = s
        return s

    def order_factory(token, **cb):
        s = _FakeOrderSocket(**cb)
        holder["order"] = s
        return s

    return holder, data_factory, order_factory


# ---- data socket --------------------------------------------------------


@pytest.mark.asyncio
async def test_data_tick_publishes_to_bus_under_short_name():
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed(["SBIN"]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: "NSE:SBIN-EQ" if s == "SBIN" else None,
    )
    mgr._loop = asyncio.get_running_loop()

    await mgr._ensure_connected()
    await mgr._reconcile_subscriptions()

    # Subscribed to the resolved FULL symbol, and the order socket armed.
    assert holder["data"].subscribed == [(("NSE:SBIN-EQ",), "SymbolUpdate")]
    assert holder["order"].subscribed == ["OnOrders,OnTrades"]

    # A tick arriving on the SDK thread lands on the bus under the SHORT key.
    mgr._on_data_message(
        {
            "symbol": "NSE:SBIN-EQ",
            "ltp": 543.2,
            "bid_price": 543.1,
            "ask_price": 543.3,
            "ch": 2.0,
            "chp": 0.37,
            "prev_close_price": 541.2,
            "vol_traded_today": 1000,
        }
    )
    await asyncio.sleep(0.05)  # let the threadsafe publish run

    q = await md.get_quote("SBIN")
    assert q is not None
    assert q.last_price == 543.2
    assert q.extra.get("source") == "fyers_ws"
    assert q.change == 2.0
    assert q.prev_close == 541.2


@pytest.mark.asyncio
async def test_unsubscribe_when_symbol_unwatched():
    md = MarketDataBus()
    holder, df, of = _factories()
    feed = _FakeQuoteFeed(["SBIN"])
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=feed,
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()
    await mgr._reconcile_subscriptions()
    assert holder["data"].subscribed[-1] == (("NSE:SBIN-EQ",), "SymbolUpdate")

    feed._symbols = []  # position closed
    await mgr._reconcile_subscriptions()
    assert holder["data"].unsubscribed == [(("NSE:SBIN-EQ",), "SymbolUpdate")]


@pytest.mark.asyncio
async def test_data_tick_tolerant_symbol_mapping():
    """The feed may echo the symbol in a different shape than the
    subscription string (split exchange + no NSE: prefix). The tick must
    still map to the subscribed short key."""
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed(["BAJAJ-AUTO"]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()
    await mgr._reconcile_subscriptions()  # NSE:BAJAJ-AUTO-EQ -> "BAJAJ-AUTO"

    mgr._on_data_message({"exchange": "NSE", "symbol": "BAJAJ-AUTO-EQ", "ltp": 9000.0})
    await asyncio.sleep(0.05)

    q = await md.get_quote("BAJAJ-AUTO")
    assert q is not None and q.last_price == 9000.0
    assert mgr._pub_count == 1 and mgr._rx_count == 1


# ---- WS-first one-shot price -------------------------------------------


@pytest.mark.asyncio
async def test_get_live_price_serves_from_bus_without_subscribing():
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed([]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()
    await md.publish("SBIN", 501.0, extra={"source": "fyers_ws"})

    price = await mgr.get_live_price("SBIN", timeout=0.5)

    assert price == 501.0
    assert holder["data"].subscribed == []  # bus-first: no subscribe needed


@pytest.mark.asyncio
async def test_get_live_price_subscribes_on_demand_and_waits_for_tick():
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed([]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()

    async def _emit():
        await asyncio.sleep(0.15)
        await md.publish("TCS", 3900.0, extra={"source": "fyers_ws"})

    asyncio.create_task(_emit())
    price = await mgr.get_live_price("TCS", timeout=1.0, poll=0.05)

    assert price == 3900.0
    assert holder["data"].subscribed == [(("NSE:TCS-EQ",), "SymbolUpdate")]


@pytest.mark.asyncio
async def test_get_live_price_times_out_to_none_for_rest_fallback():
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed([]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()

    price = await mgr.get_live_price("ZEEL", timeout=0.2, poll=0.05)

    assert price is None  # no tick → caller falls back to REST
    assert holder["data"].subscribed == [(("NSE:ZEEL-EQ",), "SymbolUpdate")]


@pytest.mark.asyncio
async def test_get_live_price_none_when_socket_not_connected():
    md = MarketDataBus()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed([]),
        backend_provider=lambda: None,  # no live account → never connects
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()

    assert await mgr.get_live_price("SBIN", timeout=0.2) is None


@pytest.mark.asyncio
async def test_ondemand_subscription_cleaned_up_when_not_a_position():
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed([]),  # nothing watched
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
        ondemand_ttl_s=0.0,  # expire immediately
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()

    await mgr.get_live_price("TCS", timeout=0.05)  # subscribes on demand
    assert "TCS" in mgr._subscribed

    await mgr._reconcile_subscriptions()  # TCS not watched + TTL lapsed

    assert "TCS" not in mgr._subscribed
    assert holder["data"].unsubscribed == [(("NSE:TCS-EQ",), "SymbolUpdate")]


@pytest.mark.asyncio
async def test_touch_interest_subscribes_once_and_refreshes_ttl():
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed([]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: s if ":" in s else f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()

    mgr.touch_interest("NSE:RELIANCE-EQ")
    assert "NSE:RELIANCE-EQ" in mgr._subscribed
    assert holder["data"].subscribed == [(("NSE:RELIANCE-EQ",), "SymbolUpdate")]
    ts1 = mgr._ondemand["NSE:RELIANCE-EQ"]

    mgr.touch_interest("NSE:RELIANCE-EQ")  # idempotent: no second subscribe
    assert holder["data"].subscribed == [(("NSE:RELIANCE-EQ",), "SymbolUpdate")]
    assert mgr._ondemand["NSE:RELIANCE-EQ"] >= ts1


@pytest.mark.asyncio
async def test_touch_interest_noop_when_not_connected():
    md = MarketDataBus()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed([]),
        backend_provider=lambda: None,  # no live account
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()
    mgr.touch_interest("SBIN")  # must not raise
    assert "SBIN" not in mgr._subscribed


# ---- order socket -------------------------------------------------------


@pytest.mark.asyncio
async def test_order_update_routes_through_reconcile():
    md = MarketDataBus()
    holder, df, of = _factories()
    calls: list[tuple] = []

    async def fake_reconcile(db, order, *, source):
        calls.append((order, source))
        return {"ok": True}

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed([]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        reconcile_fn=fake_reconcile,
        session_factory=lambda: _FakeSession(),
    )
    mgr._loop = asyncio.get_running_loop()

    # The order socket wraps the order under a dict `orders` (not a list).
    mgr._on_order_update(
        {"s": "ok", "orders": {"id": "ORD-1", "status": "2", "symbol": "NSE:SBIN-EQ", "tradedPrice": 100.0}}
    )
    await asyncio.sleep(0.05)

    assert len(calls) == 1
    order, source = calls[0]
    assert order["id"] == "ORD-1"
    assert source == "fyers_order_ws"


# ---- symbol resolution --------------------------------------------------


def test_resolve_passthrough_full_symbol():
    assert resolve_fyers_symbol("NSE:SBIN-EQ") == "NSE:SBIN-EQ"
    assert resolve_fyers_symbol("nse:sbin-eq") == "NSE:SBIN-EQ"


def test_resolve_empty_returns_none():
    assert resolve_fyers_symbol("") is None


def test_resolve_short_name_prefers_nse(monkeypatch):
    class _Hit:
        def __init__(self, symbol, short, exch):
            self.symbol = symbol
            self.short_name = short
            self.exchange = exch

    class _Master:
        def search(self, q, limit, segments):
            return [
                _Hit("BSE:SBIN-X", "SBIN", "BSE"),
                _Hit("NSE:SBIN-EQ", "SBIN", "NSE"),
            ]

    monkeypatch.setattr(
        "app.services.instrument_master.get_master", lambda: _Master()
    )
    assert resolve_fyers_symbol("SBIN") == "NSE:SBIN-EQ"


# ---- QuoteFeed staleness gate ------------------------------------------


@pytest.mark.asyncio
async def test_quote_feed_skips_rest_when_ws_tick_fresh():
    md = MarketDataBus()
    calls: list[str] = []

    async def live_fn(sym):
        calls.append(sym)
        return 999.0

    qf = QuoteFeed(market_data=md, live_quote_fn=live_fn)
    qf.watch("SBIN")
    await md.publish("SBIN", 500.0, extra={"source": "fyers_ws"})

    await qf._tick_all()

    assert calls == []  # REST poll suppressed by the fresh socket tick
    q = await md.get_quote("SBIN")
    assert q.last_price == 500.0  # WS price preserved


@pytest.mark.asyncio
async def test_quote_feed_polls_when_only_rest_tick_present():
    md = MarketDataBus()
    calls: list[str] = []

    async def live_fn(sym):
        calls.append(sym)
        return 999.0

    qf = QuoteFeed(market_data=md, live_quote_fn=live_fn)
    qf.watch("SBIN")
    # A REST-sourced tick ("fyers") must NOT suppress the poll, else the
    # feed would freeze itself.
    await md.publish("SBIN", 500.0, extra={"source": "fyers"})

    await qf._tick_all()

    assert calls == ["SBIN"]


# ---- shared reconciler --------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_order_update_fills_and_mirrors_position(db_session, isolated_db):
    from app.db.models import Position, Trade
    from app.db.session import SessionLocal

    # A symbol/order-id no other test touches — the in-memory DB is shared
    # across the session and not every test cleans up after itself.
    with SessionLocal() as s:
        s.add(
            Trade(
                symbol="ZSTREAM",
                side="BUY",
                quantity=10,
                price=600.0,
                order_type="market",
                status="placed",
                broker_order_id="STRM-ORD-9",
            )
        )
        s.commit()

    with SessionLocal() as s:
        res = await reconcile_order_update(
            s,
            {
                "id": "STRM-ORD-9",
                "status": "2",  # FILLED
                "symbol": "NSE:ZSTREAM-EQ",
                "transactionType": "BUY",
                "qty": 10,
                "tradedPrice": 612.5,
            },
            source="fyers_order_ws",
        )

    assert res["matched"] is True
    assert res["status"] == "filled"

    with SessionLocal() as s:
        t = s.query(Trade).filter_by(broker_order_id="STRM-ORD-9").one()
        assert t.status == "filled"
        assert t.price == 612.5
        pos = s.query(Position).filter_by(symbol="ZSTREAM").one()
        assert pos.quantity == 10


@pytest.mark.asyncio
async def test_reconcile_unmatched_order_is_acknowledged(db_session, isolated_db):
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        res = await reconcile_order_update(
            s, {"id": "NOPE-1", "status": "2", "symbol": "NSE:TCS-EQ"}, source="fyers_order_ws"
        )
    assert res["matched"] is False


# ---- resolution cache ---------------------------------------------------


def test_resolve_caches_successful_lookups(monkeypatch):
    """A second call for the same short name must NOT re-search the master
    (would defeat the cache). Verified by counting search() calls."""

    class _Hit:
        def __init__(self, symbol, short, exch):
            self.symbol = symbol
            self.short_name = short
            self.exchange = exch

    calls: list[str] = []

    class _Master:
        def search(self, q, limit, segments):
            calls.append(q)
            return [_Hit("NSE:SBIN-EQ", "SBIN", "NSE")]

    monkeypatch.setattr(
        "app.services.instrument_master.get_master", lambda: _Master()
    )

    assert resolve_fyers_symbol("SBIN") == "NSE:SBIN-EQ"
    assert resolve_fyers_symbol("SBIN") == "NSE:SBIN-EQ"
    assert resolve_fyers_symbol("sbin") == "NSE:SBIN-EQ"  # case-insensitive
    assert len(calls) == 1  # only the FIRST call hit the master


def test_resolve_cache_clear_forces_relookup(monkeypatch):
    """clear_resolution_cache() must drop cached results so a freshly
    patched master is consulted on the next call."""

    class _Hit:
        def __init__(self, symbol, short, exch):
            self.symbol = symbol
            self.short_name = short
            self.exchange = exch

    class _Master:
        def __init__(self, sym):
            self._sym = sym

        def search(self, q, limit, segments):
            return [_Hit(self._sym, "SBIN", "NSE")]

    # First call with the "old" master.
    monkeypatch.setattr(
        "app.services.instrument_master.get_master", lambda: _Master("NSE:SBIN-EQ")
    )
    assert resolve_fyers_symbol("SBIN") == "NSE:SBIN-EQ"

    # Swap to a "new" master (operator recreated the instrument). Without
    # clear_resolution_cache, the cached answer would be served.
    monkeypatch.setattr(
        "app.services.instrument_master.get_master", lambda: _Master("NSE:SBIN-EO")
    )
    assert resolve_fyers_symbol("SBIN") == "NSE:SBIN-EQ"  # cached

    clear_resolution_cache()
    assert resolve_fyers_symbol("SBIN") == "NSE:SBIN-EO"  # fresh


# ---- silence watchdog + unmapped cap -----------------------------------


def test_silence_watchdog_fires_when_connected_subscribed_but_no_ticks(monkeypatch):
    """After `silence_watchdog_s` of silence (no _on_data_message calls),
    the next `_log_stats()` must emit `fyers_stream.silent` at WARNING.
    Off-market this also fires — that's intentional, the operator filters."""
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed(["SBIN"]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
        silence_watchdog_s=0.5,
    )
    mgr._loop = None  # not running, we'll drive _log_stats manually
    mgr._data_connected = True
    mgr._subscribed = {"SBIN": "NSE:SBIN-EQ"}
    # Pretend we received a tick long ago, then went silent.
    mgr._last_rx_ts = 0.0  # far in the past relative to monotonic now
    mgr._last_stats_log = 0.0

    captured: list[tuple[str, str, dict]] = []

    def fake_warning(event: str, **kw):
        captured.append(("warning", event, kw))

    def fake_info(event: str, **kw):
        captured.append(("info", event, kw))

    monkeypatch.setattr("app.execution.fyers_stream.log.warning", fake_warning)
    monkeypatch.setattr("app.execution.fyers_stream.log.info", fake_info)

    mgr._log_stats()

    events = [c for c in captured if c[1] == "fyers_stream.silent"]
    assert len(events) == 1, events
    level, _, kwargs = events[0]
    assert level == "warning"
    assert kwargs["threshold_s"] == 0.5
    assert kwargs["subscribed"] == 1
    # rx_stats must also reflect silent=True so dashboards can render it.
    stats = [c for c in captured if c[1] == "fyers_stream.rx_stats"]
    assert stats[0][2]["silent"] is True


def test_silence_watchdog_does_not_fire_before_first_tick():
    """During the startup window before the market opens (no tick yet),
    the watchdog must stay quiet — `_last_rx_ts is None`."""
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed(["SBIN"]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
        silence_watchdog_s=0.0,  # would fire instantly if allowed
    )
    mgr._loop = None
    mgr._data_connected = True
    mgr._subscribed = {"SBIN": "NSE:SBIN-EQ"}
    mgr._last_rx_ts = None  # never received a tick
    mgr._last_stats_log = 0.0

    import structlog

    class _Capture(structlog.stdlib.BoundLogger):
        def __init__(self):
            self.warnings: list[tuple[str, dict]] = []
            self.infos: list[tuple[str, dict]] = []

        def warning(self, event, **kw):
            self.warnings.append((event, kw))

        def info(self, event, **kw):
            self.infos.append((event, kw))

        # everything else is a no-op
        def __getattr__(self, name):
            return lambda *a, **k: None

    cap = _Capture()
    mgr._log_stats.__globals__  # noqa: B018 — sanity
    # Patch log inside the module so _log_stats picks up our capture.
    import app.execution.fyers_stream as mod

    real_warning = mod.log.warning
    real_info = mod.log.info
    mod.log.warning = cap.warning
    mod.log.info = cap.info
    try:
        mgr._log_stats()
    finally:
        mod.log.warning = real_warning
        mod.log.info = real_info

    assert all(e != "fyers_stream.silent" for e, _ in cap.warnings)


def test_unmapped_tick_logs_warning_with_drop_count():
    """Each unmapped tick must increment drop_count and log at WARNING
    (not info) so a parser-broken condition surfaces in production logs."""
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed(["SBIN"]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )

    import app.execution.fyers_stream as mod

    captured: list[tuple[str, str, dict]] = []

    def fake_warning(event, **kw):
        captured.append(("warning", event, kw))

    def fake_info(event, **kw):
        captured.append(("info", event, kw))

    real_w, real_i = mod.log.warning, mod.log.info
    mod.log.warning = fake_warning
    mod.log.info = fake_info
    try:
        # No subscription covers this exchange/symbol combo.
        mgr._on_data_message({"symbol": "NSE:UNKNOWN-EQ", "exchange": "NSE", "ltp": 100.0})
        mgr._on_data_message({"symbol": "NSE:ALSO-UNKNOWN-EQ", "exchange": "NSE", "ltp": 100.0})
    finally:
        mod.log.warning = real_w
        mod.log.info = real_i

    unmapped = [c for c in captured if c[1] == "fyers_stream.unmapped_tick"]
    assert len(unmapped) == 2
    for level, _, kw in unmapped:
        assert level == "warning"
        assert "drop_count" in kw
        assert kw["reason"] == "symbol_unresolved"
    assert mgr._drop_count == 2


def test_unmapped_tick_suppression_after_cap():
    """After `_UNMAPPED_LOG_CAP` individual warnings, a single summary
    line fires once and subsequent drops are silent (counted only)."""
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed(["SBIN"]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    cap = FyersStreamManager._UNMAPPED_LOG_CAP

    import app.execution.fyers_stream as mod

    captured: list[tuple[str, str]] = []

    def fake_warning(event, **kw):
        captured.append(("warning", event))

    real_w = mod.log.warning
    mod.log.warning = fake_warning
    try:
        # Drive `cap + 5` drops so we cross the boundary.
        for i in range(cap + 5):
            mgr._on_data_message({"symbol": f"NSE:X{i}-EQ", "exchange": "NSE", "ltp": 1.0})
    finally:
        mod.log.warning = real_w

    individual = [c for c in captured if c[1] == "fyers_stream.unmapped_tick"]
    summary = [c for c in captured if c[1] == "fyers_stream.unmapped_tick_suppressed"]
    assert len(individual) == cap  # exactly cap individual warnings
    assert len(summary) == 1       # exactly one summary line at cap+1
    assert mgr._drop_count == cap + 5


# ---- reconnect subscription-loss fix -----------------------------------


@pytest.mark.asyncio
async def test_data_close_clears_subscriptions_so_reconcile_resubscribes():
    """The Fyers SDK's __on_close wipes `scrips_per_channel` and
    `symbol_token` before auto-reconnecting, which means server-side
    subscriptions are lost after a network blip. Our reconcile loop must
    detect that and re-subscribe — verified by checking `_subscribed` is
    cleared on `_on_data_close` and the next reconcile re-emits the
    subscribe call."""
    md = MarketDataBus()
    holder, df, of = _factories()
    feed = _FakeQuoteFeed(["SBIN", "TCS"])
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=feed,
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()
    await mgr._reconcile_subscriptions()

    # Initial subscribe went out (order doesn't matter — _subscribed is
    # populated from a set() iteration which is order-arbitrary).
    assert len(holder["data"].subscribed) == 1
    symbols0 = set(holder["data"].subscribed[0][0])
    assert symbols0 == {"NSE:SBIN-EQ", "NSE:TCS-EQ"}
    assert set(mgr._subscribed.keys()) == {"SBIN", "TCS"}

    # Simulate an SDK auto-reconnect: fire on_close (server-side subs are
    # now wiped) then on_connect (socket is back, but we don't know that
    # until reconcile notices _subscribed is empty).
    mgr._on_data_close()
    assert mgr._subscribed == {}, "_subscribed must be cleared on close"
    assert mgr._full_to_short == {}, "_full_to_short must be cleared on close"
    mgr._on_data_connect()
    assert mgr._data_connected is True

    # Next reconcile sweep must re-subscribe everything.
    holder["data"].subscribed.clear()
    await mgr._reconcile_subscriptions()

    # The SDK call must include both full broker ids.
    assert len(holder["data"].subscribed) == 1
    symbols1 = set(holder["data"].subscribed[0][0])
    assert symbols1 == {"NSE:SBIN-EQ", "NSE:TCS-EQ"}
    assert set(mgr._subscribed.keys()) == {"SBIN", "TCS"}


@pytest.mark.asyncio
async def test_data_close_preserves_ondemand_ttls():
    """`_ondemand` carries time-based TTLs for transient subscriptions
    (e.g. one-shot get_live_price for an announcement symbol). Clearing
    it on close would let those unsubscribe the instant after reconnect
    even though the user is still waiting for a tick — must NOT clear."""
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed([]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()

    mgr.touch_interest("NSE:RELIANCE-EQ")
    assert "NSE:RELIANCE-EQ" in mgr._ondemand

    mgr._on_data_close()

    assert "NSE:RELIANCE-EQ" in mgr._ondemand, "_ondemand must survive close"
    assert mgr._subscribed == {}


@pytest.mark.asyncio
async def test_order_connect_resubscribes_after_sdk_reconnect():
    """The order socket's `OnOrders,OnTrades` subscription is also wiped
    by the SDK's auto-reconnect. Verify `_on_order_connect` re-sends it
    so we don't miss fill updates after a network blip."""
    md = MarketDataBus()
    holder, df, of = _factories()
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=_FakeQuoteFeed([]),
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()

    # _connect_sockets already subscribed once during ensure_connected.
    assert holder["order"].subscribed == ["OnOrders,OnTrades"]

    # Simulate SDK auto-reconnect.
    mgr._on_order_close()
    holder["order"].subscribed.clear()
    mgr._on_order_connect()

    # _on_order_connect must re-send the subscription.
    assert holder["order"].subscribed == ["OnOrders,OnTrades"]


@pytest.mark.asyncio
async def test_reconcile_does_not_crash_when_close_clears_subs_mid_iteration():
    """Concurrency guard: `_on_data_close` runs on the SDK thread and
    can clear `_subscribed` while the event loop is iterating it inside
    `_reconcile_subscriptions`. The snapshot fix must keep this from
    raising RuntimeError."""
    md = MarketDataBus()
    holder, df, of = _factories()
    feed = _FakeQuoteFeed(["SBIN"])
    mgr = FyersStreamManager(
        market_data=md,
        quote_feed=feed,
        backend_provider=lambda: _FakeBackend(),
        data_socket_factory=df,
        order_socket_factory=of,
        resolve_fn=lambda s: f"NSE:{s}-EQ",
    )
    mgr._loop = asyncio.get_running_loop()
    await mgr._ensure_connected()
    await mgr._reconcile_subscriptions()
    assert "SBIN" in mgr._subscribed

    # Simulate the race: SDK thread clears _subscribed just before the
    # event loop starts the next reconcile iteration.
    mgr._on_data_close()
    mgr._on_data_connect()
    mgr._data_connected = True

    # Must not raise (would have raised RuntimeError pre-fix).
    await mgr._reconcile_subscriptions()

    assert "SBIN" in mgr._subscribed  # re-subscribed after the race
    # The subscribe list grew: initial reconcile, then reconcile after the
    # simulated close→connect. Both emit SBIN.
    assert (("NSE:SBIN-EQ",), "SymbolUpdate") in holder["data"].subscribed
