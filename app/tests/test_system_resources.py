"""The resources endpoint must never raise and must warn at the threshold.

It feeds the 09:05 preflight alarm, so a crash here would swallow the DB
and Fyers checks that run beside it.
"""
from __future__ import annotations

from app.api import system


def test_resources_shape_and_never_raises() -> None:
    r = system.system_resources()
    # Disk works on every platform; memory is None off Linux.
    assert set(r) >= {"memory", "disk", "dirs", "files", "thresholds", "warnings"}
    assert r["disk"]["total_gb"] is None or r["disk"]["total_gb"] > 0
    if r["memory"] is not None:
        m = r["memory"]
        assert m["total_mb"] > 0
        assert 0 <= m["used_pct"] <= 100
        # used + available must reconstruct the total, not drift.
        assert abs(m["used_mb"] + m["available_mb"] - m["total_mb"]) < 1.0


def test_warns_when_over_threshold(monkeypatch) -> None:
    """A 99%-full box must produce warnings; a 1%-full one must not."""
    monkeypatch.setattr(
        system, "_meminfo",
        lambda: {
            "total_mb": 1000.0, "available_mb": 10.0, "used_mb": 990.0,
            "used_pct": 99.0, "cached_mb": 0.0, "swap_total_mb": 2000.0,
            "swap_used_mb": 1800.0, "swap_used_pct": 90.0,
        },
    )
    monkeypatch.setattr(
        system, "_disk",
        lambda: {"total_gb": 58.0, "used_gb": 57.0, "free_gb": 1.0, "used_pct": 98.0},
    )
    warnings = system.system_resources()["warnings"]
    assert len(warnings) == 3, warnings          # memory, swap, disk
    assert any("Memory" in w for w in warnings)
    assert any("Swap" in w for w in warnings)
    assert any("Disk" in w for w in warnings)

    monkeypatch.setattr(
        system, "_meminfo",
        lambda: {
            "total_mb": 1000.0, "available_mb": 900.0, "used_mb": 100.0,
            "used_pct": 10.0, "cached_mb": 0.0, "swap_total_mb": 2000.0,
            "swap_used_mb": 0.0, "swap_used_pct": 0.0,
        },
    )
    monkeypatch.setattr(
        system, "_disk",
        lambda: {"total_gb": 58.0, "used_gb": 6.0, "free_gb": 52.0, "used_pct": 10.0},
    )
    assert system.system_resources()["warnings"] == []


def test_memory_breakdown_sums_to_total() -> None:
    """A breakdown you can draw as a bar must add up, or it lies."""
    r = system.system_resources()
    if r["memory"] is None:
        return  # no /proc on this host (Windows dev box)
    m = r["memory"]
    parts = m["breakdown"]
    assert [p["key"] for p in parts] == ["apps", "cache", "slab", "free"]
    assert abs(sum(p["mb"] for p in parts) - m["total_mb"]) < 2.0
    assert all(p["mb"] >= -1.0 for p in parts)
    # Only `apps` survives memory pressure; the rest the kernel can take.
    assert [p["reclaimable"] for p in parts] == [False, True, True, True]


def test_top_processes_are_sorted_and_bounded() -> None:
    procs = system._top_processes(5)
    if not procs:
        return  # no /proc
    assert len(procs) <= 5
    rss = [p["rss_mb"] for p in procs]
    assert rss == sorted(rss, reverse=True)
    assert all(p["name"] and p["pid"] > 0 for p in procs)
