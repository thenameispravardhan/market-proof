"""The single unified dataset: one DuckDB file, one table, one schema.

Everything lives in `announcements` inside data/warehouse.duckdb:

    the 276,115-row historical dataset          (loaded from nse_master.parquet)
    every announcement the live bot has collected (schema-mapped from SQLite)
    every announcement collected from now on      (inserted on arrival)

A DuckDB table rather than a pile of parquet files, because the ask is row-level
inserts as news arrives AND fast analytical queries. Parquet cannot do the
former; SQLite is slow at the latter over 280k rows x 91 columns. DuckDB does
both from a single file, and still streams columns off disk rather than loading
the table into RAM.

IDENTITY. `event_id` is the historical dataset's key and only means anything for
the rows that came from it. The real key is `uid` — exchange + symbol +
announcement time + a content hash — which is stable across both sources and is
what dedupes a live row against a historical one when their ranges overlap.

PRICE COLUMNS. A freshly collected filing has no outcome yet, so px_*/vol_*/
day_* arrive NULL and `price_status` says 'pending'. warehouse_prices.py fills
them once the candles exist. Nothing about ingestion waits for prices.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

# The live SQLite stores UTC; the historical dataset stores IST. Verified by
# joining the overlap window: 15,743 rows match with identical headlines once
# live is shifted +5:30. Without this every overlapping filing would be stored
# twice, at two different times, under two different uids.
IST_SHIFT = "INTERVAL 5 HOUR + INTERVAL 30 MINUTE"

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "warehouse.duckdb"
MASTER = ROOT / "AIdataset" / "labels" / "nse_master.parquet"
LIVE_DB = ROOT / "data" / "trading.db"

TABLE = "announcements"
_lock = threading.Lock()
_local = threading.local()

# The 91 columns of the historical dataset, plus provenance. Everything the
# unified table holds is declared here so the schema has exactly one definition.
PRICE_POINTS = ["pre", "t0", "1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m",
                "10m", "11m", "12m", "13m", "14m", "15m", "20m", "30m", "45m", "60m"]
HORIZONS = [5, 15, 30, 60]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    uid              VARCHAR PRIMARY KEY,
    event_id         BIGINT,
    seq_id           BIGINT,
    exchange         VARCHAR,
    symbol           VARCHAR,
    company          VARCHAR,
    isin             VARCHAR,
    cap_tier         VARCHAR,
    market_cap_cr    DOUBLE,
    mcap_rank        DOUBLE,
    is_smallcap      BOOLEAN,
    announced_at     TIMESTAMP,
    disseminated_at  TIMESTAMP,
    category         VARCHAR,
    headline         VARCHAR,
    attachment_url   VARCHAR,
    attachment_size  VARCHAR,
    content_hash     VARCHAR,
    anchor_time      TIMESTAMP,
    session_offset   VARCHAR,
    {", ".join(f"px_{p} DOUBLE, vol_{p} DOUBLE" for p in PRICE_POINTS)},
    day_high         DOUBLE,
    day_low          DOUBLE,
    day_volume       DOUBLE,
    px_t0_age_min    DOUBLE,
    data_quality     VARCHAR,
    price_source     VARCHAR,
    price_status     VARCHAR DEFAULT 'pending',
    {", ".join(f"ret_{h}m DOUBLE, mkt_{h}m DOUBLE, adj_{h}m DOUBLE" for h in HORIZONS)},
    usable           BOOLEAN,
    mover_1_5        BOOLEAN,
    mover_3          BOOLEAN,
    ai_event_type    VARCHAR,
    ai_sentiment     VARCHAR,
    ai_sentiment_score DOUBLE,
    ai_confidence    DOUBLE,
    ai_recommendation VARCHAR,
    ai_summary       VARCHAR,
    ai_reasoning     VARCHAR,
    ai_label_source  VARCHAR,
    ai_label_shared_by BIGINT,
    ai_model         VARCHAR,
    ai_prompt_version VARCHAR,
    source_layer     VARCHAR,
    ingested_at      TIMESTAMP DEFAULT current_timestamp
);
"""

# `uid` must be computed identically wherever a row enters, or the same
# announcement lands twice under two different keys.
#
# The base is exchange + symbol + second + headline hash. That is NOT unique on
# its own: NSE routinely publishes several attachments in the same second under
# one headline (one group has 9), and 1,638 historical rows collide that way.
# They are genuinely distinct filings and must not be collapsed, so an ordinal
# is appended — assigned by row_number() within the group, ordered by the
# source's own id. Both sides order by their own stable id, so the Nth filing of
# a group maps to the same uid whether it arrives from history or from live.
UID_BASE = ("lower(coalesce({ex},'NSE')) || ':' || upper({sym}) || ':' || "
            "strftime(CAST({ts} AS TIMESTAMP), '%Y%m%d%H%M%S') || ':' || "
            "substr(md5(coalesce({hl},'')), 1, 10)")


def connect(read_only: bool = False):
    con = getattr(_local, "con", None)
    if con is not None:
        return con
    import duckdb

    STORE.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(STORE), read_only=read_only)
    # Bounded: this shares a 2 GB box with a live trading process.
    con.execute("SET memory_limit='256MB'; SET threads=2;")
    # A full-table export sorts 303k x 97, which does not fit in 256 MB and
    # raised OutOfMemory. Giving DuckDB a spill directory lets it page the sort
    # to disk instead — the box has 47 GB free and 256 MB of RAM to spare, so
    # trading against disk for memory is the right way round here.
    tmp = STORE.parent / "duckdb_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp.as_posix()}'")
    con.execute("SET preserve_insertion_order=false")
    if not read_only:
        con.execute(SCHEMA)
        con.execute(f"CREATE INDEX IF NOT EXISTS idx_sym ON {TABLE}(symbol)")
        con.execute(f"CREATE INDEX IF NOT EXISTS idx_at  ON {TABLE}(announced_at)")
    _local.con = con
    return con


def close() -> None:
    con = getattr(_local, "con", None)
    if con is not None:
        con.close()
        _local.con = None


def load_history() -> int:
    """Load the historical dataset. Idempotent — existing uids are skipped."""
    con = connect()
    base = UID_BASE.format(ex="'NSE'", sym="symbol", ts="announced_at", hl="headline")
    cols = [c[0] for c in con.execute(
        f"describe select * from '{MASTER.as_posix()}'").fetchall()]
    shared = [c for c in cols if c in _table_columns(con)]
    sel = ", ".join(f"s.{c}" for c in shared)
    with _lock:
        n = con.execute(f"""
            INSERT INTO {TABLE} (uid, exchange, source_layer, price_status, {", ".join(shared)})
            SELECT s.uid, 'NSE', 'history',
                   CASE WHEN s.px_t0 IS NULL THEN 'pending' ELSE 'filled' END, {sel}
            FROM (
                SELECT *, {base} || ':' ||
                       row_number() OVER (PARTITION BY {base} ORDER BY seq_id) AS uid
                FROM '{MASTER.as_posix()}'
            ) s
            WHERE s.uid NOT IN (SELECT uid FROM {TABLE})
        """).fetchone()
    log.info("warehouse_store.history_loaded", rows=n[0] if n else 0)
    return n[0] if n else 0


def load_live() -> int:
    """Map every SQLite announcement onto the unified schema and insert it.

    This is the schema unification: the live table's 11 columns become the
    dataset's 91, with prices left pending and the AI fields taken from the
    analyses row when the filing was analysed.
    """
    con = connect()
    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{LIVE_DB.as_posix()}' AS live (TYPE sqlite, READ_ONLY)")
    # Every live timestamp is converted to IST before it is used, both for the
    # uid and for the stored value, so the two sources land on one clock.
    ist_filed = f"(CAST(a.filed_at AS TIMESTAMP) + {IST_SHIFT})"
    ist_recv = f"(CAST(a.received_at AS TIMESTAMP) + {IST_SHIFT})"
    base = UID_BASE.format(ex="a.exchange", sym="a.symbol",
                          ts=ist_filed, hl="a.headline")
    try:
        with _lock:
            n = con.execute(f"""
                INSERT INTO {TABLE} (
                    uid, event_id, exchange, symbol, headline, category,
                    announced_at, disseminated_at, attachment_url, content_hash,
                    ai_event_type, ai_sentiment, ai_sentiment_score, ai_confidence,
                    ai_recommendation, ai_reasoning, ai_model,
                    ai_label_source, source_layer, price_status)
                SELECT * FROM (
                    SELECT {base} || ':' ||
                           row_number() OVER (PARTITION BY {base} ORDER BY a.id) AS uid,
                           a.id, coalesce(a.exchange,'NSE'), a.symbol, a.headline,
                           a.event_type, {ist_filed},
                           {ist_recv}, a.pdf_url, a.content_hash,
                           a.event_type,
                           an.sentiment, an.sentiment_score, an.confidence,
                           an.recommendation, an.rationale, an.model,
                           CASE WHEN an.id IS NULL THEN NULL ELSE 'live' END,
                           'live', 'pending'
                    FROM live.announcements a
                    LEFT JOIN live.analyses an ON an.announcement_id = a.id
                ) m
                WHERE m.uid NOT IN (SELECT uid FROM {TABLE})
            """).fetchone()
    finally:
        con.execute("DETACH live")
    log.info("warehouse_store.live_loaded", rows=n[0] if n else 0)
    return n[0] if n else 0


def _table_columns(con) -> set[str]:
    return {r[0] for r in con.execute(f"describe {TABLE}").fetchall()}


def insert_announcement(row: dict[str, Any]) -> bool:
    """Insert one freshly collected announcement. Returns False if already present.

    Called from the live collector the moment a filing arrives — prices and the
    AI label are absent at that point and get filled in place later.
    """
    con = connect()
    cols = _table_columns(con)
    payload = {k: v for k, v in row.items() if k in cols and k != "uid"}
    if not payload.get("symbol") or not payload.get("announced_at"):
        raise ValueError("symbol and announced_at are required")
    names = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    uid = UID_SQL.format(ex="?", sym="?", ts="?", hl="?")
    with _lock:
        r = con.execute(
            f"INSERT INTO {TABLE} (uid, {names}) SELECT {uid}, {marks} "
            f"WHERE {uid} NOT IN (SELECT uid FROM {TABLE})",
            [payload.get("exchange", "NSE"), payload["symbol"],
             payload["announced_at"], payload.get("headline", "")]
            + list(payload.values())
            + [payload.get("exchange", "NSE"), payload["symbol"],
               payload["announced_at"], payload.get("headline", "")],
        ).fetchone()
    return bool(r and r[0])


def ingest_live(*, symbol: str, headline: str, announced_at, exchange: str = "NSE",
                category: Optional[str] = None, attachment_url: Optional[str] = None,
                content_hash: Optional[str] = None, disseminated_at=None,
                event_id: Optional[int] = None) -> bool:
    """Insert a freshly collected announcement straight into the dataset.

    Called from the monitor the moment a filing is stored in SQLite. Prices and
    the AI label are absent at that point — they are filled in place later by
    warehouse_prices and the analyzer, so nothing here waits on them.

    `announced_at` must already be IST. The monitors work in UTC, so the caller
    converts; storing UTC here would silently create a duplicate of the same
    filing 5h30m away from its historical twin.
    """
    con = connect()
    base = UID_BASE.format(ex="?", sym="?", ts="?", hl="?")
    key = [exchange or "NSE", symbol, announced_at, headline or ""]
    with _lock:
        # content_hash is the live system's own dedup key and is the right one
        # here: the ordinal alone cannot tell "a second filing in the same
        # second" from "the same filing delivered twice" — it would just hand
        # the repeat the next ordinal and store it again.
        if content_hash:
            dup = con.execute(
                f"SELECT 1 FROM {TABLE} WHERE content_hash = ? LIMIT 1",
                [content_hash]).fetchone()
            if dup:
                return False
        # Ordinal for genuinely new content. Streaming rows arrive after the
        # historical cutoff, so there is no historical twin to collide with —
        # that overlap was a one-time concern, handled in load_live().
        nth = con.execute(
            f"SELECT count(*) + 1 FROM {TABLE} WHERE uid LIKE {base} || ':%'", key
        ).fetchone()[0]
        r = con.execute(f"""
            INSERT INTO {TABLE}
                (uid, event_id, exchange, symbol, headline, category, announced_at,
                 disseminated_at, attachment_url, content_hash, source_layer, price_status)
            SELECT {base} || ':' || {nth}, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', 'pending'
            WHERE {base} || ':' || {nth} NOT IN (SELECT uid FROM {TABLE})
        """, key + [event_id, exchange or "NSE", symbol, headline, category,
                    announced_at, disseminated_at, attachment_url, content_hash] + key
        ).fetchone()
    return bool(r and r[0])


def apply_analysis(uid_or_event_id: Any, analysis: dict[str, Any]) -> int:
    """Attach an AI label to a row already in the dataset (analysis arrives later)."""
    con = connect()
    fields = {f"ai_{k}": v for k, v in analysis.items()
              if f"ai_{k}" in _table_columns(con)}
    if not fields:
        return 0
    sets = ", ".join(f"{k} = ?" for k in fields)
    col = "uid" if isinstance(uid_or_event_id, str) else "event_id"
    with _lock:
        con.execute(f"UPDATE {TABLE} SET {sets} WHERE {col} = ?",
                    list(fields.values()) + [uid_or_event_id])
    return 1


def stats() -> dict[str, Any]:
    con = connect()
    q = lambda s: con.execute(s).fetchone()  # noqa: E731
    total = q(f"select count(*) from {TABLE}")[0]
    return {
        "store": str(STORE),
        "size_mb": round(STORE.stat().st_size / 1e6, 1) if STORE.exists() else 0,
        "rows": total,
        "by_source": dict(con.execute(
            f"select source_layer, count(*) from {TABLE} group by 1").fetchall()),
        "price_status": dict(con.execute(
            f"select price_status, count(*) from {TABLE} group by 1").fetchall()),
        "with_ai_label": q(f"select count(*) from {TABLE} where ai_sentiment is not null")[0],
        "with_prices": q(f"select count(*) from {TABLE} where px_t0 is not null")[0],
        "date_range": [str(x) for x in q(
            f"select min(announced_at), max(announced_at) from {TABLE}")],
    }


if __name__ == "__main__":
    import json
    load_history()
    load_live()
    print(json.dumps(stats(), indent=2, default=str))
