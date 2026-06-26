"""Unit tests for the volatility-aware entry levels, the anti-chase guard,
and ATR-driven trailing (Phases 1-3 wiring in the execution layer)."""
from __future__ import annotations

from app.execution.manager import Manager
from app.execution.market_data import MarketDataBus
from app.execution.trade_manager import ManagedPosition


class _StubATR:
    """A VolatilityProvider that returns a fixed ATR for any symbol."""

    def __init__(self, atr: float) -> None:
        self._atr = atr

    def atr(self, symbol: str):  # noqa: D401
        return self._atr


def _manager(vol_provider=None) -> Manager:
    return Manager(market_data=MarketDataBus(), vol_provider=vol_provider)


# -- ATR-aware level derivation ------------------------------------------


def test_derive_levels_uses_atr():
    mgr = _manager(vol_provider=_StubATR(10.0))
    # ORDER_WIN profile: sl_atr_mult 2.0, target_rr 3.5, hold 1500.
    stop, target, rr, hold = mgr._derive_levels(
        symbol="RELIANCE", entry=1000.0, action="BUY", event_type="ORDER_WIN",
    )
    assert abs(stop - 980.0) < 1e-6        # 1000 - (ATR 10 × 2)
    assert abs(target - 1070.0) < 1e-6      # 1000 + 20 × 3.5
    assert rr == 3.5
    assert hold == 1500


def test_derive_levels_short_flips_direction():
    mgr = _manager(vol_provider=_StubATR(10.0))
    stop, target, rr, _hold = mgr._derive_levels(
        symbol="RELIANCE", entry=1000.0, action="SELL", event_type="ORDER_WIN",
    )
    assert abs(stop - 1020.0) < 1e-6        # short stop is ABOVE entry
    assert abs(target - 930.0) < 1e-6       # 1000 - 20 × 3.5


def test_derive_levels_falls_back_to_pct_without_atr():
    mgr = _manager()  # NullVolatilityProvider → no ATR
    # DIVIDEND profile: sl_default_pct 3.0, target_rr 2.0, hold 600.
    stop, target, rr, hold = mgr._derive_levels(
        symbol="RELIANCE", entry=1000.0, action="BUY", event_type="DIVIDEND",
    )
    assert abs(stop - 970.0) < 1e-6         # 3% of 1000
    assert abs(target - 1060.0) < 1e-6      # 1000 + 30 × 2.0
    assert rr == 2.0
    assert hold == 600


# -- anti-chase guard -----------------------------------------------------


def test_entry_drift_blocks_extended_long():
    mgr = _manager()
    # +2% vs intended 100 → over the 1.5% default → blocked.
    block = mgr._entry_drift_block(action="BUY", intended=100.0, current=102.0)
    assert block is not None and block[0] == "ENTRY_DRIFT"


def test_entry_drift_allows_small_move():
    mgr = _manager()
    assert mgr._entry_drift_block(action="BUY", intended=100.0, current=101.0) is None


def test_entry_drift_short_is_symmetric():
    mgr = _manager()
    # Short: favourable = price already fell. 100 → 98 is +2% favourable.
    block = mgr._entry_drift_block(action="SELL", intended=100.0, current=98.0)
    assert block is not None and block[0] == "ENTRY_DRIFT"


def test_entry_drift_missing_price_skips():
    mgr = _manager()
    assert mgr._entry_drift_block(action="BUY", intended=None, current=102.0) is None
    assert mgr._entry_drift_block(action="BUY", intended=100.0, current=None) is None


# -- ATR-driven trailing (R = ATR distance) -------------------------------


def test_trailing_ratchets_off_atr_risk():
    # Entry 1000, ATR-derived stop 980 → R = 20.
    mp = ManagedPosition(
        symbol="RELIANCE", quantity=10, entry=1000.0, stop_loss=980.0,
        target=1070.0, initial_risk=20.0,
        trail_activate_r=1.5, trail_distance_r=0.5,
    )
    # Price reaches +1.5R (1030) → trailing arms and ratchets the stop to
    # peak - 0.5R = 1030 - 10 = 1020.
    moved = mp.apply_trailing(1030.0)
    assert moved is True
    assert mp.trail_active is True
    assert abs(mp.stop_loss - 1020.0) < 1e-6
