"""A model-declared index must exist even on a table created without it.

`create_all` builds indexes only when it builds the table, so a column
added later via _ADDED_COLUMNS (ALTER TABLE) silently loses its index.
That cost 9s per HOLD-calibration request in production.
"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app.db.init import _ensure_indexes
from app.db.session import Base


def test_creates_a_declared_index_missing_from_an_existing_table(tmp_path):
    url = f"sqlite:///{tmp_path / 'idx.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)

    # Simulate the production state: the index the model declares was
    # never created, because the column arrived by ALTER TABLE.
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_dataset_features_announcement_id"))
    before = {i["name"] for i in inspect(engine).get_indexes("dataset_features")}
    assert "ix_dataset_features_announcement_id" not in before

    _ensure_indexes(engine)

    after = {i["name"] for i in inspect(engine).get_indexes("dataset_features")}
    assert "ix_dataset_features_announcement_id" in after

    # Idempotent: a second run must not raise on the now-present index.
    _ensure_indexes(engine)
    engine.dispose()


def test_never_raises_on_a_broken_engine():
    """Startup must survive a migration hiccup, not die on one."""
    _ensure_indexes(object())  # not an engine; must be swallowed
