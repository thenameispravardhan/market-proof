// Resources tile — live RAM, swap and storage for the server.
//
// The box is a 2 GB Lightsail instance and both of its exhaustible
// resources have already cost a trading day: uvicorn was OOM-killed on
// 2026-08-15, and the SQLite file has corrupted three times. Neither was
// visible from the dashboard until the damage was done — this tile is the
// gauge that was missing.
//
// Flexibility the operator actually needs, and nothing more:
//   - a poll-interval picker (1s .. paused), remembered in localStorage,
//     because 5s polling all day on an idle box is pure waste;
//   - a breakdown toggle for per-directory / per-file storage, since the
//     top-line disk % never tells you WHAT grew.
// Warn thresholds live in Settings (RESOURCE_WARN_*) — the same numbers
// drive the 09:05 preflight alarm, so screen and alarm cannot disagree.

import { useEffect, useState } from "react";
import { useSystemResources } from "../../hooks/useApi";
import { Skeleton } from "./Skeleton";

const INTERVALS: { label: string; ms: number }[] = [
  { label: "1s", ms: 1000 },
  { label: "5s", ms: 5000 },
  { label: "15s", ms: 15000 },
  { label: "60s", ms: 60000 },
  { label: "Paused", ms: 0 },
];

const STORAGE_KEY = "resources.intervalMs";

function gb(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${v.toFixed(1)} GB`;
}

function mb(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v >= 1024 ? `${(v / 1024).toFixed(2)} GB` : `${Math.round(v)} MB`;
}

// Green until two-thirds of the warn threshold, amber approaching it, red
// past it. Anchoring to the operator's own threshold (rather than a fixed
// 80/90) means retuning Settings retunes the colors with it.
function barClass(pct: number | null | undefined, warnAt: number): string {
  if (pct === null || pct === undefined) return "";
  if (pct >= warnAt) return "pnl-neg";
  if (pct >= warnAt * 0.8) return "lat-warn";
  return "pnl-pos";
}

function Bar({
  label,
  pct,
  detail,
  warnAt,
}: {
  label: string;
  pct: number | null | undefined;
  detail: string;
  warnAt: number;
}) {
  const cls = barClass(pct, warnAt);
  const width = Math.min(100, Math.max(0, pct ?? 0));
  return (
    <div className="res-row">
      <div className="res-head">
        <span className="lat-label">{label}</span>
        <span className={`mono ${cls}`}>
          {pct === null || pct === undefined ? "—" : `${pct.toFixed(1)}%`}
        </span>
      </div>
      <div
        className="res-track"
        role="meter"
        aria-label={label}
        aria-valuenow={pct ?? 0}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={`${pct ?? 0}% — ${detail}`}
      >
        <div className={`res-fill ${cls}`} style={{ width: `${width}%` }} />
      </div>
      <div className="meta">{detail}</div>
    </div>
  );
}

export function SystemResources() {
  const [intervalMs, setIntervalMs] = useState<number>(() => {
    const saved = Number(localStorage.getItem(STORAGE_KEY));
    return INTERVALS.some((i) => i.ms === saved) ? saved : 5000;
  });
  const [showBreakdown, setShowBreakdown] = useState(false);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, String(intervalMs));
  }, [intervalMs]);

  const { data, isLoading, error } = useSystemResources(intervalMs);

  const mem = data?.memory;
  const disk = data?.disk;
  const warnMem = data?.thresholds.mem_pct ?? 85;
  const warnDisk = data?.thresholds.disk_pct ?? 85;

  return (
    <div className="widget" data-testid="system-resources">
      <h3>
        Resources
        <select
          className="res-interval"
          value={intervalMs}
          onChange={(e) => setIntervalMs(Number(e.target.value))}
          title="How often to poll. Paused stops the requests entirely."
          aria-label="Refresh interval"
        >
          {INTERVALS.map((i) => (
            <option key={i.ms} value={i.ms}>
              {i.label}
            </option>
          ))}
        </select>
      </h3>

      {isLoading ? (
        <Skeleton height={150} />
      ) : error ? (
        <p className="empty">Failed to read resources: {error.message}</p>
      ) : !data ? (
        <p className="empty">No data.</p>
      ) : (
        <div className="lat-body">
          {data.warnings.map((w) => (
            <p className="res-warn" key={w}>
              {w}
            </p>
          ))}

          {mem ? (
            <>
              <Bar
                label="Memory"
                pct={mem.used_pct}
                warnAt={warnMem}
                detail={`${mb(mem.used_mb)} of ${mb(mem.total_mb)} used · ${mb(
                  mem.available_mb,
                )} available · ${mb(mem.cached_mb)} cache`}
              />
              {mem.swap_total_mb > 0 ? (
                <Bar
                  label="Swap"
                  pct={mem.swap_used_pct}
                  warnAt={50}
                  detail={`${mb(mem.swap_used_mb)} of ${mb(
                    mem.swap_total_mb,
                  )} — sustained swap means the box is too small`}
                />
              ) : null}
            </>
          ) : (
            <p className="empty">
              Memory stats need /proc — unavailable on this host (dev box).
            </p>
          )}

          <Bar
            label="Disk"
            pct={disk?.used_pct}
            warnAt={warnDisk}
            detail={`${gb(disk?.used_gb)} of ${gb(disk?.total_gb)} used · ${gb(
              disk?.free_gb,
            )} free`}
          />

          <div className="lat-stats">
            <div className="lat-stat">
              <span className="lat-label">Bot process</span>
              <span className="mono">{mb(data.process_rss_mb)}</span>
            </div>
            {data.files.map((f) => (
              <div className="lat-stat" key={f.name}>
                <span className="lat-label">{f.name}</span>
                <span className="mono">{mb(f.size_mb)}</span>
              </div>
            ))}
          </div>

          <button
            className="ghost btn-sm"
            onClick={() => setShowBreakdown((v) => !v)}
            aria-expanded={showBreakdown}
          >
            {showBreakdown ? "Hide" : "Show"} storage breakdown
          </button>
          {showBreakdown ? (
            <table>
              <thead>
                <tr>
                  <th>Directory</th>
                  <th style={{ textAlign: "right" }}>Size</th>
                </tr>
              </thead>
              <tbody>
                {data.dirs.map((d) => (
                  <tr key={d.name}>
                    <td className="mono">{d.name}</td>
                    <td className="mono" style={{ textAlign: "right" }}>
                      {mb(d.size_mb)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          {showBreakdown ? (
            <p className="meta">Directory sizes are cached for 60s.</p>
          ) : null}
        </div>
      )}
    </div>
  );
}
