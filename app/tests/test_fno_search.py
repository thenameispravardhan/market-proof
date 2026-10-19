"""F&O search: options come from the Fyers F&O master as compact rows; several
words ("NIFTY 23100 PE", "NIFTY FUT") match by underlying, strike and side."""
from __future__ import annotations

import time

from app.services.instrument_master import InstrumentMaster


def _row(sym, short, lot, tick, exp, strike="-1.0", opt="XX"):
    # Fyers sym_details layout: 3 lot, 4 tick, 8 expiry, 9 symbol, 13 short, 15 strike, 16 CE/PE/XX
    r = [""] * 21
    r[1], r[3], r[4], r[8], r[9], r[13], r[15], r[16] = sym, str(lot), str(tick), str(exp), sym, short, strike, opt
    return ",".join(r)


def test_option_and_future_words(tmp_path):
    near, far = int(time.time()) + 3 * 86400, int(time.time()) + 30 * 86400
    rows = [
        _row("NSE:NIFTY26OCTFUT", "NIFTY", 65, 0.1, far),
        _row("NSE:BANKNIFTY26OCTFUT", "BANKNIFTY", 30, 0.2, far),
        _row("NSE:NIFTY26NOV23100PE", "NIFTY", 65, 0.05, far, "23100.0", "PE"),
        _row("NSE:NIFTY26O1923100PE", "NIFTY", 65, 0.05, near, "23100.0", "PE"),
        _row("NSE:NIFTY26O1923100CE", "NIFTY", 65, 0.05, near, "23100.0", "CE"),
        _row("NSE:NIFTY26O1922000PE", "NIFTY", 65, 0.05, int(time.time()) - 5 * 86400, "22000.0", "PE"),  # expired
    ]
    (tmp_path / "NSE_FO.csv").write_text("\n".join(rows), encoding="utf-8")
    m = InstrumentMaster(tmp_path)
    assert [i.symbol for i in m.search("NIFTY 23100 PE", limit=5)][:2] == ["NSE:NIFTY26O1923100PE", "NSE:NIFTY26NOV23100PE"]
    assert m.search("NIFTY23100CE", limit=5)[0].symbol == "NSE:NIFTY26O1923100CE"
    assert m.search("NIFTY FUT", limit=5)[0].symbol == "NSE:NIFTY26OCTFUT"
    assert not any(i.symbol.endswith("22000PE") for i in m.search("NIFTY 22000 PE", limit=5))
    o = m.get("NSE:NIFTY26O1923100PE")
    assert o is not None and (o.lot_size, o.tick_size, o.strike) == (65, 0.05, 23100.0)
    from app.services.instrument_master import search_filter
    opts = m.search("NIFTY", limit=5, where=search_filter(types=["OPT"]))
    assert opts and all(i.instrument_type in ("CE", "PE") for i in opts)
