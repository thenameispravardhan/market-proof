"""F&O lot sizes: read per contract from the Fyers F&O scrip master, never
guessed for stocks, and enforced on manual orders."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.algo import fno
from app.execution.fyers_live import normalize_option_chain
from app.tests.test_trade_page import _install_stub_backend, real_account  # noqa: F401 — fixture

NOW = time.time()
NEAR = int(NOW + 7 * 86400)       # a live expiry this month
FAR = int(NOW + 60 * 86400)       # an expiry listed after a lot revision
GONE = int(NOW - 30 * 86400)      # an expired contract still in an old file


def _row(symbol: str, name: str, lot: int, expiry: int, strike: float = -1, opt: str = "XX") -> list[str]:
    """One NSE_FO.csv row in Fyers' sym_details layout (see fno.master)."""
    return ["1", symbol, "14", str(lot), "0.05", "", "0915-1530", "", str(expiry), symbol,
            "10", "11", "1", name, "1", str(strike), opt, "", ""]


@pytest.fixture()
def fo_master(tmp_path, monkeypatch):
    rows = [
        # NIFTY mid-revision: this month's contracts still trade in 75s, the
        # newly listed far month in 65s.
        _row("NSE:NIFTY25OCT25000CE", "NIFTY", 75, NEAR, 25000, "CE"),
        _row("NSE:NIFTY25DEC25000CE", "NIFTY", 65, FAR, 25000, "CE"),
        _row("NSE:NIFTY25OCTFUT", "NIFTY", 75, NEAR),
        _row("NSE:BANKNIFTY25OCT56000PE", "BANKNIFTY", 35, NEAR, 56000, "PE"),
        _row("NSE:NIFTYNXT5025OCT70000CE", "NIFTYNXT50", 25, NEAR, 70000, "CE"),
        _row("NSE:SBIN25SEP800CE", "SBIN", 1500, GONE, 800, "CE"),
        _row("NSE:SBIN25OCT800CE", "SBIN", 750, NEAR, 800, "CE"),
        _row("NSE:SBIN25OCTFUT", "SBIN", 750, NEAR),
        _row("NSE:M&M25OCT3500CE", "M&M", 200, NEAR, 3500, "CE"),
    ]
    (tmp_path / "NSE_FO.csv").write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(fno, "MASTER_DIR", tmp_path)
    monkeypatch.setitem(fno._cache, "mtime", None)
    yield tmp_path
    fno._cache.update(mtime=None, data=fno._EMPTY)


@pytest.fixture()
def no_master(tmp_path, monkeypatch):
    monkeypatch.setattr(fno, "MASTER_DIR", tmp_path / "missing")
    monkeypatch.setitem(fno._cache, "mtime", None)
    yield
    fno._cache.update(mtime=None, data=fno._EMPTY)


@pytest.mark.parametrize("symbol,name", [
    ("NSE:NIFTY25O1424500CE", "NIFTY"),          # weekly
    ("NSE:NIFTY2561424500PE", "NIFTY"),          # weekly, numeric month
    ("NSE:BANKNIFTY25OCT56000PE", "BANKNIFTY"),  # monthly
    ("BSE:SENSEX25O1681000CE", "SENSEX"),
    ("NSE:SBIN25OCTFUT", "SBIN"),
    ("NSE:BAJAJ-AUTO25OCT9000CE", "BAJAJ-AUTO"),
    ("NSE:NIFTY25OCT24550.5CE", "NIFTY"),
])
def test_underlying_of_parses_fyers_derivative_symbols(no_master, symbol, name):
    assert fno.underlying_of(symbol) == name


@pytest.mark.parametrize("symbol", ["NSE:SBIN-EQ", "NSE:NIFTY50-INDEX", "NSE:NIFTYBEES-EQ",
                                    "MCX:CRUDEOIL25OCTFUT", "SBIN", ""])
def test_cash_index_and_non_nse_bse_are_not_derivatives(no_master, symbol):
    assert fno.underlying_of(symbol) is None
    assert fno.lot_error(symbol, 7) is None


def test_name_ending_in_digits_resolves_against_the_master(fo_master):
    assert fno.underlying_of("NSE:NIFTYNXT5025OCT70000CE") == "NIFTYNXT50"
    assert fno.contract_lot("NSE:NIFTYNXT5025OCT70000CE") == 25


def test_each_contract_keeps_its_own_lot_through_a_revision(fo_master):
    assert fno.contract_lot("NSE:NIFTY25OCT25000CE") == 75
    assert fno.contract_lot("NSE:NIFTY25DEC25000CE") == 65
    # Underlying-level lot = the nearest live expiry, not the first file row
    # (SBIN's expired 1500 row comes first).
    assert fno.known_lot("NIFTY") == 75
    assert fno.known_lot("SBIN") == 750
    assert fno.lot_size("SBIN") == 750
    # A contract the master doesn't list yet falls back to its underlying.
    assert fno.contract_lot("NSE:SBIN25NOV820CE") == 750
    assert fno.contract_lot("NSE:M&M25OCT3500CE") == 200


def test_master_keeps_per_contract_lots_only_for_revised_underlyings(fo_master):
    contracts = fno.master()["contracts"]
    assert "NSE:NIFTY25DEC25000CE" in contracts
    assert "NSE:BANKNIFTY25OCT56000PE" not in contracts


def test_lot_error_rejects_non_multiples(fo_master):
    assert fno.lot_error("NSE:SBIN25OCT800CE", 1500) is None
    msg = fno.lot_error("NSE:SBIN25OCT800CE", 1)
    assert msg and "multiple of 750" in msg
    # NIFTY 65 is not a lot of an October contract (75), but is for December.
    assert fno.lot_error("NSE:NIFTY25OCT25000CE", 65)
    assert fno.lot_error("NSE:NIFTY25DEC25000CE", 65) is None


def test_stock_lot_is_never_guessed_without_the_master(no_master):
    assert fno.contract_lot("NSE:SBIN25OCT800CE") is None
    msg = fno.lot_error("NSE:SBIN25OCT800CE", 1)
    assert msg and "unknown" in msg
    # Indices still have the current exchange lots to fall back on.
    assert fno.contract_lot("NSE:BANKNIFTY25OCT56000PE") == fno.INDEX_LOTS["BANKNIFTY"]
    # Currency F&O isn't in the equity F&O master; it passes through.
    assert fno.lot_error("NSE:USDINR25OCTFUT", 3) is None


def test_stock_unknown_to_a_loaded_master_is_refused(fo_master):
    msg = fno.lot_error("NSE:NEWCO25OCT100CE", 1)
    assert msg and "isn't in the F&O scrip master" in msg


def _chain(sym_prefix: str) -> dict:
    return {"data": {"optionsChain": [
        {"symbol": "NSE:SBIN-EQ", "ltp": 800, "option_type": "", "strike_price": -1},
        {"symbol": f"{sym_prefix}800CE", "ltp": 12, "option_type": "CE", "strike_price": 800},
    ], "expiryData": []}}


def test_option_chain_stock_lot_is_null_not_one_without_master(no_master):
    out = normalize_option_chain(_chain("NSE:SBIN25OCT"), underlying_symbol="NSE:SBIN-EQ")
    assert out["strikes"][0]["ce"]["lot_size"] is None


def test_option_chain_uses_the_contracts_own_lot(fo_master):
    out = normalize_option_chain(
        {"data": {"optionsChain": [
            {"symbol": "NSE:NIFTY25DEC25000CE", "ltp": 300, "option_type": "CE", "strike_price": 25000},
        ], "expiryData": []}},
        underlying_symbol="NSE:NIFTY50-INDEX",
    )
    assert out["strikes"][0]["ce"]["lot_size"] == 65


def _order(client: TestClient, account_id: int, symbol: str, qty: int):
    return client.post("/api/orders", json={
        "account_id": account_id, "symbol": symbol, "side": "BUY", "quantity": qty,
        "order_type": "MARKET", "product_type": "INTRADAY",
    })


def test_place_order_refuses_a_non_lot_quantity(client, isolated_db, real_account, fo_master):  # noqa: F811
    _install_stub_backend(client, real_account.id, ok=True)
    r = _order(client, real_account.id, "NSE:SBIN25OCT800CE", 100)
    assert r.status_code == 422 and "multiple of 750" in r.json()["detail"]
    r = _order(client, real_account.id, "NSE:SBIN25OCT800CE", 750)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_place_order_fetches_the_master_before_refusing(client, isolated_db, real_account,  # noqa: F811
                                                        monkeypatch, fo_master):
    """Unknown only because the master isn't downloaded: fetch it, then check."""
    calls = []

    async def fake_ensure(*a, **k):
        calls.append(1)
        monkeypatch.setattr(fno, "MASTER_DIR", fo_master)
        fno._cache["mtime"] = None

    monkeypatch.setattr(fno, "MASTER_DIR", fo_master / "missing")
    monkeypatch.setattr(fno, "ensure_master", fake_ensure)
    _install_stub_backend(client, real_account.id, ok=True)
    r = _order(client, real_account.id, "NSE:SBIN25OCT800CE", 750)
    assert calls and r.status_code == 200, r.text


def test_place_order_refuses_unknown_stock_lot(client, isolated_db, real_account,  # noqa: F811
                                               no_master, monkeypatch):
    async def fake_ensure(*a, **k):
        return None

    monkeypatch.setattr(fno, "ensure_master", fake_ensure)
    _install_stub_backend(client, real_account.id, ok=True)
    r = _order(client, real_account.id, "NSE:SBIN25OCT800CE", 1)
    assert r.status_code == 422 and "unknown" in r.json()["detail"]


def test_lot_endpoint(client, fo_master):
    assert client.get("/api/options/lot", params={"symbol": "NSE:SBIN-EQ"}).json()["lot_size"] == 1
    j = client.get("/api/options/lot", params={"symbol": "nse:nifty25dec25000ce"}).json()
    assert j["lot_size"] == 65 and j["underlying"] == "NIFTY" and j["derivative"] is True
    j = client.get("/api/options/lots",
                   params={"symbols": "NSE:SBIN-EQ, NSE:NIFTY25OCT25000CE,NSE:NEWCO25OCT100CE"}).json()
    assert j["lots"] == {"NSE:SBIN-EQ": 1, "NSE:NIFTY25OCT25000CE": 75, "NSE:NEWCO25OCT100CE": None}
