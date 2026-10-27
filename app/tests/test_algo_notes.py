"""Backtest notes: a message every symbol shares is one line, and a blown account says when."""
from app.api.algo import _fold_notes, _ran_out_note


def test_shared_note_folds_into_one_line_and_rare_ones_stay():
    budget = "Fyers data budget for automations used up today"
    notes = [{"symbol": f"NSE:S{i}-EQ", "note": budget} for i in range(6)] + [{"symbol": "NSE:X-EQ", "note": "no candles"}]
    out = _fold_notes(notes)
    assert out == [{"symbol": "6 symbols (S0-EQ, S1-EQ, S2-EQ, S3-EQ, …)", "note": budget},
                   {"symbol": "NSE:X-EQ", "note": "no candles"}]
    assert _fold_notes(notes[:2]) == notes[:2]          # two symbols: kept as they are


def test_ran_out_note_names_the_day_the_capital_went():
    day = 1735689600                                     # 2025-01-01 00:00 UTC
    res = {"stats": {"skipped": {"no_capital": 120}},
           "trades": [{"exit_t": day + 86400 * 2, "net": -60000.0}, {"exit_t": day + 86400 * 5, "net": -39500.0}]}
    note = _ran_out_note(res, 100000)
    assert note["symbol"] == "portfolio" and "2025-01-06" in note["note"] and "120 entries" in note["note"]
    assert _ran_out_note({**res, "stats": {"skipped": {"no_capital": 0}}}, 100000) is None
    assert _ran_out_note({**res, "trades": res["trades"][:1]}, 100000) is None   # 40% left: not run out
