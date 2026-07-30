"""One query surface over everything: history (parquet) + live (SQLite).

WHY NOT REPLACE SQLITE
----------------------
Measured on this machine: the live path's queries run in 2-4 ms over 141,001
rows. SQLite is not the bottleneck, and it is the transactional store for money
- single writer, ACID, proven. Swapping it would be risk with no payoff.

What IS slow is analytics over the 276,115-row historical dataset. That is an
OLAP problem, and DuckDB is the right tool: it streams parquet from disk instead
of loading it, so the same aggregate that costs pandas ~700 MB of RAM costs
DuckDB ~15 MB. On the 2 GB Lightsail box (503 MB free) that difference is the
difference between working and an OOM kill.

So: SQLite keeps the writes, DuckDB serves the reads, and this module joins them
into a single logical dataset. The SQLite attachment is READ_ONLY - analytics
physically cannot lock or corrupt the trading database.

    from app.services.warehouse import query
    query("select ai_event_type, avg(adj_30m) from history where usable group by 1")
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
LIVE_DB = ROOT / "data" / "trading.db"
# The historical dataset. A directory of parquet files is read as one table, so
# new months append as new files instead of rewriting the 86 MB base.
WAREHOUSE = ROOT / "AIdataset" / "warehouse"
MASTER = ROOT / "AIdataset" / "labels" / "nse_master.parquet"

_local = threading.local()


def _history_source() -> str:
    """A DuckDB FROM-clause covering the base dataset plus every increment.

    `union_by_name` lets the increments carry fewer columns than the 91-column
    base — a freshly collected announcement has no price outcome yet, and those
    columns simply arrive as NULL rather than forcing a rewrite of the base file.
    """
    parts = [f"'{MASTER.as_posix()}'"]
    if WAREHOUSE.exists():
        inc = sorted(WAREHOUSE.glob("*.parquet"))
        parts += [f"'{p.as_posix()}'" for p in inc]
    if len(parts) == 1:
        return f"read_parquet({parts[0]})"
    return f"read_parquet([{', '.join(parts)}], union_by_name=true)"


def connect(read_only_live: bool = True):
    """A thread-local DuckDB connection with history + live registered.

    Thread-local because DuckDB connections are not thread-safe, and the API
    serves concurrent requests.
    """
    con = getattr(_local, "con", None)
    if con is not None:
        return con

    import duckdb

    con = duckdb.connect(database=":memory:")
    # Keep DuckDB's own memory bounded — this runs beside a trading process on a
    # 2 GB box, and an unbounded hash join would take the whole instance down.
    con.execute("SET memory_limit='256MB'; SET threads=2;")
    con.execute("INSTALL sqlite; LOAD sqlite;")

    con.execute(f"CREATE OR REPLACE VIEW history AS SELECT * FROM {_history_source()}")

    if LIVE_DB.exists():
        mode = ", READ_ONLY" if read_only_live else ""
        con.execute(f"ATTACH '{LIVE_DB.as_posix()}' AS live (TYPE sqlite{mode})")
        # Live announcements mapped onto the history column names, so the two
        # can be UNIONed without the caller knowing which side a row came from.
        con.execute("""
            CREATE OR REPLACE VIEW live_news AS
            SELECT a.id                AS event_id,
                   a.symbol            AS symbol,
                   a.headline          AS headline,
                   a.event_type        AS category,
                   a.filed_at          AS announced_at,
                   a.received_at       AS disseminated_at,
                   a.pdf_url           AS attachment_url,
                   an.sentiment        AS ai_sentiment,
                   an.sentiment_score  AS ai_sentiment_score,
                   an.confidence       AS ai_confidence,
                   an.recommendation   AS ai_recommendation,
                   an.rationale        AS ai_reasoning,
                   'live'              AS source_layer
            FROM live.announcements a
            LEFT JOIN live.analyses an ON an.announcement_id = a.id
        """)
        # The unified feed. History wins on overlap: it carries the price
        # outcomes and the market-adjusted labels, which live rows do not have.
        con.execute("""
            CREATE OR REPLACE VIEW news AS
            SELECT event_id, symbol, headline, category, announced_at,
                   ai_sentiment, ai_sentiment_score, ai_confidence,
                   ai_recommendation, adj_30m, mover_1_5, 'history' AS source_layer
            FROM history
            UNION ALL
            SELECT event_id, symbol, headline, category, announced_at,
                   ai_sentiment, ai_sentiment_score, ai_confidence,
                   ai_recommendation, NULL, NULL, source_layer
            FROM live_news l
            WHERE l.announced_at > (SELECT max(announced_at) FROM history)
        """)
    _local.con = con
    return con


def query(sql: str, params: Optional[list[Any]] = None):
    """Run SQL against the unified warehouse. Returns a list of tuples."""
    return connect().execute(sql, params or []).fetchall()


def df(sql: str, params: Optional[list[Any]] = None):
    """Same, as a DataFrame. Only materialises the RESULT, not the source."""
    return connect().execute(sql, params or []).fetch_df()


def stats() -> dict[str, Any]:
    con = connect()
    out: dict[str, Any] = {"source": _history_source()[:120]}
    out["history_rows"] = con.execute("select count(*) from history").fetchone()[0]
    try:
        out["live_rows"] = con.execute("select count(*) from live_news").fetchone()[0]
        out["unified_rows"] = con.execute("select count(*) from news").fetchone()[0]
        out["history_max"] = str(con.execute("select max(announced_at) from history").fetchone()[0])
        out["live_max"] = str(con.execute("select max(announced_at) from live_news").fetchone()[0])
    except Exception as e:  # noqa: BLE001 — live DB absent in tests
        out["live_error"] = str(e)[:120]
    return out


def close() -> None:
    con = getattr(_local, "con", None)
    if con is not None:
        con.close()
        _local.con = None


if __name__ == "__main__":
    import json
    print(json.dumps(stats(), indent=2, default=str))
