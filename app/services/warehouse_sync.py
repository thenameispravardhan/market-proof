"""Fold newly collected announcements into the historical dataset.

The live bot writes to SQLite (fast, transactional, safe). This lifts anything
newer than the dataset's high-water mark into a parquet increment, so the
warehouse's `history` view keeps growing without the trading path ever knowing
about it.

Increments are separate files per month, never a rewrite of the 86 MB base:
appending 200 rows should cost 200 rows of work, not 276,115. DuckDB reads the
base and every increment as one table via union_by_name.

Everything runs INSIDE DuckDB — the rows are read from SQLite and written to
parquet without passing through pandas, so memory stays flat regardless of how
much has accumulated. That matters on the 2 GB server.

    python -m app.services.warehouse_sync          # append what is new
    python -m app.services.warehouse_sync --dry    # report only
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.logging_config import get_logger
from app.services.warehouse import MASTER, WAREHOUSE, connect, _history_source

log = get_logger(__name__)

# Columns an increment carries. A newly collected filing has no price outcome
# yet, so the ret/mkt/adj columns are simply absent and arrive as NULL.
INCREMENT_SELECT = """
    SELECT a.id                                   AS event_id,
           a.symbol                               AS symbol,
           a.headline                             AS headline,
           a.event_type                           AS category,
           CAST(a.filed_at AS TIMESTAMP)          AS announced_at,
           CAST(a.received_at AS TIMESTAMP)       AS disseminated_at,
           a.pdf_url                              AS attachment_url,
           a.exchange                             AS exchange,
           a.content_hash                         AS content_hash,
           an.model                               AS ai_model,
           an.sentiment                           AS ai_sentiment,
           an.sentiment_score                     AS ai_sentiment_score,
           an.confidence                          AS ai_confidence,
           an.recommendation                      AS ai_recommendation,
           an.rationale                           AS ai_reasoning,
           'live'                                 AS ai_label_source
    FROM live.announcements a
    LEFT JOIN live.analyses an ON an.announcement_id = a.id
    WHERE CAST(a.filed_at AS TIMESTAMP) > ?
"""


def high_water_mark(con) -> datetime:
    return con.execute("SELECT max(announced_at) FROM history").fetchone()[0]


def sync(dry: bool = False) -> dict:
    con = connect()
    hwm = high_water_mark(con)
    n = con.execute(
        f"SELECT count(*) FROM ({INCREMENT_SELECT})", [hwm]
    ).fetchone()[0]

    out = {"high_water_mark": str(hwm), "new_rows": n}
    if n == 0 or dry:
        log.info("warehouse_sync.dry" if dry else "warehouse_sync.nothing_new", **out)
        return out

    WAREHOUSE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = WAREHOUSE / f"live_{stamp}.parquet"

    # COPY streams straight from SQLite to parquet — nothing is materialised in
    # Python, so peak memory is independent of row count.
    con.execute(
        f"COPY ({INCREMENT_SELECT}) TO '{target.as_posix()}' "
        "(FORMAT parquet, COMPRESSION zstd)", [hwm]
    )
    # Rebuild the view so the new increment is visible immediately.
    con.execute(f"CREATE OR REPLACE VIEW history AS SELECT * FROM {_history_source()}")
    total = con.execute("SELECT count(*) FROM history").fetchone()[0]

    out.update({"written": target.name,
                "size_kb": round(target.stat().st_size / 1024, 1),
                "history_rows_now": total})
    log.info("warehouse_sync.appended", **out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    r = sync(dry=a.dry)
    for k, v in r.items():
        print(f"  {k:<20} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
