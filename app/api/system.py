"""GET /api/system/resources — live RAM, swap and storage for the host.

Why this exists: the bot runs on a 2 GB Lightsail box and has been
OOM-killed once (2026-08-15, uvicorn at 1.5 GB anon-rss) and has corrupted
its SQLite file three times. Both failures were invisible from the
dashboard until the trading day was already lost. This endpoint puts the
two exhaustible resources — memory and disk — on screen while they are
still headroom rather than an outage.

Stdlib only, on purpose: `/proc` + `shutil.disk_usage` answer the whole
question, and psutil would be a new dependency for a page of arithmetic.
Every field is Optional — on Windows (the dev box) `/proc` does not
exist, so the memory block comes back null and the disk block still works.

Directory sizes are the expensive part (AIdataset/ alone is ~2,000 files),
so they are cached for _DIR_TTL_S. Memory and disk are free to read and
are never cached.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter

from app.config import get_settings
from app.logging_config import get_logger

router = APIRouter(prefix="/api/system", tags=["system"])
log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]

# Directories worth watching: the ones that GROW. Anything static
# (.venv, node_modules, .git) is noise on a storage page.
WATCHED_DIRS: tuple[tuple[str, Path], ...] = (
    ("data", ROOT / "data"),
    ("data/backups", ROOT / "data" / "backups"),
    ("logs", ROOT / "logs"),
    ("AIdataset", ROOT / "AIdataset"),
)

# Individual files big enough to matter on their own.
WATCHED_FILES: tuple[tuple[str, Path], ...] = (
    ("trading.db", ROOT / "data" / "trading.db"),
    ("trading.db-wal", ROOT / "data" / "trading.db-wal"),
    ("warehouse.duckdb", ROOT / "data" / "warehouse.duckdb"),
)

_DIR_TTL_S = 60.0
_dir_cache: dict[str, Any] = {"at": 0.0, "dirs": None}


def _meminfo() -> Optional[dict[str, float]]:
    """RAM + swap from /proc/meminfo, in MB. None off Linux.

    MemAvailable (not MemFree) is the number that matters — it counts
    reclaimable page cache, so a box with 200 MB free and 800 MB of cache
    is healthy, not dying. Reporting MemFree would cry wolf every day.
    """
    try:
        raw = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    kb: dict[str, float] = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                kb[key] = float(parts[0])
            except ValueError:
                continue
    if "MemTotal" not in kb:
        return None
    total = kb["MemTotal"] / 1024
    available = kb.get("MemAvailable", kb.get("MemFree", 0.0)) / 1024
    swap_total = kb.get("SwapTotal", 0.0) / 1024
    swap_free = kb.get("SwapFree", 0.0) / 1024

    # A breakdown whose four parts SUM TO TOTAL, which is the only way a
    # bar chart of it is honest. The split that matters operationally is
    # reclaimable vs not: `apps` is the only part the kernel cannot take
    # back under pressure, so it is the number that predicts an OOM.
    free = kb.get("MemFree", 0.0) / 1024
    buffers = kb.get("Buffers", 0.0) / 1024
    cached = kb.get("Cached", 0.0) / 1024
    shmem = kb.get("Shmem", 0.0) / 1024
    sreclaim = kb.get("SReclaimable", 0.0) / 1024
    # Cached includes Shmem, which is NOT reclaimable page cache — it is
    # tmpfs/shared memory and behaves like an allocation. Move it to apps.
    page_cache = cached + buffers - shmem
    apps = total - free - page_cache - sreclaim

    return {
        "total_mb": round(total, 1),
        "available_mb": round(available, 1),
        "used_mb": round(total - available, 1),
        "used_pct": round((total - available) / total * 100, 1) if total else 0.0,
        "cached_mb": round(cached, 1),
        "breakdown": [
            {"key": "apps", "label": "Processes",
             "mb": round(apps, 1), "reclaimable": False},
            {"key": "cache", "label": "Page cache",
             "mb": round(page_cache, 1), "reclaimable": True},
            {"key": "slab", "label": "Kernel cache",
             "mb": round(sreclaim, 1), "reclaimable": True},
            {"key": "free", "label": "Free",
             "mb": round(free, 1), "reclaimable": True},
        ],
        "swap_total_mb": round(swap_total, 1),
        "swap_used_mb": round(swap_total - swap_free, 1),
        "swap_used_pct": (
            round((swap_total - swap_free) / swap_total * 100, 1) if swap_total else 0.0
        ),
    }


def _top_processes(n: int = 5) -> list[dict[str, Any]]:
    """The n largest processes by resident memory.

    Straight from /proc — no `ps` subprocess. Answers the question the
    total cannot: when memory climbs, is it the bot or something else?
    """
    out: list[tuple[float, str, int]] = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    page_mb = os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
    for pid in pids:
        try:
            # statm field 2 is resident set size, in pages.
            rss = int(open(f"/proc/{pid}/statm").read().split()[1]) * page_mb
            if rss < 1.0:
                continue
            name = open(f"/proc/{pid}/comm").read().strip()
        except (OSError, ValueError, IndexError):
            continue
        out.append((rss, name, int(pid)))
    out.sort(reverse=True)
    return [
        {"name": name, "pid": pid, "rss_mb": round(rss, 1)}
        for rss, name, pid in out[:n]
    ]


def _process_rss_mb() -> Optional[float]:
    """This process's resident memory. The bot's OWN share of the box.

    Separating it from system memory answers the question a host-level
    number cannot: is the bot leaking, or is something else on the box
    eating the RAM?
    """
    try:
        raw = Path("/proc/self/status").read_text()
    except OSError:
        return None
    for line in raw.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return round(float(parts[1]) / 1024, 1)
                except ValueError:
                    return None
    return None


def _disk() -> dict[str, Any]:
    """Usage of the volume the project lives on. Works everywhere."""
    try:
        total, used, free = shutil.disk_usage(ROOT)
    except OSError as e:  # noqa: BLE001
        log.warning("system.disk_usage_failed", error=str(e))
        return {"total_gb": None, "used_gb": None, "free_gb": None, "used_pct": None}
    gb = 1024**3
    return {
        "total_gb": round(total / gb, 1),
        "used_gb": round(used / gb, 1),
        "free_gb": round(free / gb, 1),
        "used_pct": round(used / total * 100, 1) if total else None,
    }


def _dir_size_mb(path: Path) -> Optional[float]:
    """Recursive size in MB, or None if the directory is absent.

    ponytail: os.walk with no parallelism and no incremental cache — a
    full walk of AIdataset/ is ~2,000 stats and takes well under a second.
    If a future corpus makes that hurt, cache per-directory mtimes.
    """
    if not path.is_dir():
        return None
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        for name in filenames:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return round(total / 1024 / 1024, 1)


def _dirs_cached() -> list[dict[str, Any]]:
    now = time.monotonic()
    if _dir_cache["dirs"] is not None and now - _dir_cache["at"] < _DIR_TTL_S:
        return _dir_cache["dirs"]
    dirs = [
        {"name": name, "size_mb": _dir_size_mb(path)}
        for name, path in WATCHED_DIRS
    ]
    _dir_cache["at"] = now
    _dir_cache["dirs"] = dirs
    return dirs


def _file_size_mb(path: Path) -> Optional[float]:
    try:
        return round(path.stat().st_size / 1024 / 1024, 1)
    except OSError:
        return None


@router.get("/resources")
def system_resources() -> dict[str, Any]:
    """RAM, swap, disk and the directories/files that actually grow.

    Cheap enough to poll every few seconds: the only non-trivial work is
    the directory walk, and that is cached for a minute.
    """
    settings = get_settings()
    mem = _meminfo()
    disk = _disk()

    warn_mem = float(getattr(settings, "RESOURCE_WARN_MEM_PCT", 85.0))
    warn_disk = float(getattr(settings, "RESOURCE_WARN_DISK_PCT", 85.0))

    warnings: list[str] = []
    if mem and mem["used_pct"] >= warn_mem:
        warnings.append(
            f"Memory {mem['used_pct']}% used (warn at {warn_mem:g}%) — "
            "an OOM kill takes the bot down mid-session."
        )
    if mem and mem["swap_total_mb"] and mem["swap_used_pct"] >= 50.0:
        warnings.append(
            f"Swap {mem['swap_used_pct']}% used — the box is paging; "
            "latency-sensitive entries will drift."
        )
    if disk["used_pct"] is not None and disk["used_pct"] >= warn_disk:
        warnings.append(
            f"Disk {disk['used_pct']}% used (warn at {warn_disk:g}%) — "
            "SQLite corrupts when it cannot write."
        )

    return {
        "memory": mem,
        "top_processes": _top_processes(),
        "process_rss_mb": _process_rss_mb(),
        "disk": disk,
        "dirs": _dirs_cached(),
        "files": [
            {"name": name, "size_mb": _file_size_mb(path)}
            for name, path in WATCHED_FILES
        ],
        "thresholds": {"mem_pct": warn_mem, "disk_pct": warn_disk},
        "warnings": warnings,
    }
