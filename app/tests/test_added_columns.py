"""Every model column must reach an EXISTING database: a column added to a
model without an _ADDED_COLUMNS entry breaks every query on the live DB
(tests build tables fresh, so nothing else catches it)."""
from app.db.init import _ADDED_COLUMNS
from app.db.models import Position, Trade


def test_new_trade_and_position_columns_are_migrated():
    for model, table in ((Trade, "trades"), (Position, "positions")):
        migrated = {n for n, _ in _ADDED_COLUMNS.get(table, [])}
        for col in ("product",) + (("filled_qty",) if model is Trade else ()):
            assert col in migrated, f"{table}.{col} missing from _ADDED_COLUMNS"
