// PnL chart: realised P&L over a configurable window.
//
// Realised P&L is reconstructed from /api/trades with the SAME FIFO
// round-trip logic the Trade History page uses (`buildRoundTrips`), so
// the two views always agree — they used to diverge because the chart
// summed raw per-trade pnl over every trade (incl. open entry legs and
// non-filled rows) while Trade History paired legs into round-trips.
//
// Customisable (persisted in localStorage under `dashboard.pnl.prefs`):
//   - Range:  7 / 14 / 30 / 90 days (default 14)
//   - View:   cumulative area (seeded with realised P&L from before the
//             window, so the last point equals the all-time realised
//             total on Trade History) or per-day bars
//   - Scope:  all accounts / paper only / live only — so simulated P&L
//             never pollutes the real-money read (and vice versa)

import { useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useBrokerAccounts, useTrades } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { buildRoundTrips } from "../../lib/roundtrips";
import { Skeleton } from "./Skeleton";
import type { BrokerAccount, Trade } from "../../types";

interface PnlPoint {
  date: string;
  realized: number;
  cumulative: number;
}

type RangeDays = 7 | 14 | 30 | 90;
type ViewMode = "cumulative" | "daily";
type Scope = "all" | "paper" | "live";

interface PnlPrefs {
  days: RangeDays;
  view: ViewMode;
  scope: Scope;
}

const RANGES: RangeDays[] = [7, 14, 30, 90];
const PREFS_KEY = "dashboard.pnl.prefs";
const DEFAULT_PREFS: PnlPrefs = { days: 14, view: "cumulative", scope: "all" };

const POS = "#3fb950";
const NEG = "#f85149";

function loadPrefs(): PnlPrefs {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return DEFAULT_PREFS;
    const p = JSON.parse(raw) as Partial<PnlPrefs>;
    return {
      days: RANGES.includes(p.days as RangeDays) ? (p.days as RangeDays) : DEFAULT_PREFS.days,
      view: p.view === "daily" ? "daily" : "cumulative",
      scope: p.scope === "paper" || p.scope === "live" ? p.scope : "all",
    };
  } catch {
    /* localStorage unavailable or corrupt — fall back to defaults */
    return DEFAULT_PREFS;
  }
}

function ymd(d: Date): string {
  return d.toISOString().slice(0, 10);
}

// Build the daily realised-P&L series from FIFO round-trips. Each
// closed round-trip's realised P&L is bucketed on its exit date. The
// cumulative line carries forward all realised P&L — including trades
// closed before the visible window — so its last point matches the
// Trade History realised total (for the selected account scope).
function buildSeries(
  trades: Trade[],
  byId: Map<number, BrokerAccount>,
  days: number,
  scope: Scope
): PnlPoint[] {
  const today = new Date();
  today.setUTCHours(0, 0, 0, 0);
  const start = new Date(today);
  start.setUTCDate(start.getUTCDate() - (days - 1));

  const buckets = new Map<string, number>();
  for (let i = 0; i < days; i++) {
    const d = new Date(start);
    d.setUTCDate(d.getUTCDate() + i);
    buckets.set(ymd(d), 0);
  }

  let preWindow = 0; // realised P&L from round-trips closed before the window
  for (const r of buildRoundTrips(trades, byId)) {
    if (r.open || r.pnl == null || !r.exitTime) continue;
    if (scope === "paper" && !r.isPaper) continue;
    if (scope === "live" && r.isPaper) continue;
    const d = new Date(r.exitTime);
    if (Number.isNaN(d.getTime())) continue;
    d.setUTCHours(0, 0, 0, 0);
    if (d < start) {
      preWindow += r.pnl;
      continue;
    }
    const key = ymd(d);
    if (!buckets.has(key)) continue;
    buckets.set(key, (buckets.get(key) ?? 0) + r.pnl);
  }

  let cum = preWindow;
  const out: PnlPoint[] = [];
  for (const [date, realized] of buckets) {
    cum += realized;
    out.push({ date, realized: Math.round(realized * 100) / 100, cumulative: Math.round(cum * 100) / 100 });
  }
  return out;
}

function shortDate(s: string): string {
  // YYYY-MM-DD → MMM D
  const d = new Date(s + "T00:00:00Z");
  if (Number.isNaN(d.getTime())) return s;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

function rupees(v: number): string {
  return `₹${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

export function PnLChart() {
  const [prefs, setPrefs] = useState<PnlPrefs>(loadPrefs);
  useEffect(() => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      /* private mode — the choice just won't survive a reload */
    }
  }, [prefs]);

  // Filled-only, same as Trade History. The default 500-row window is
  // Trade History's; widen it for the long ranges so 90 days of round
  // trips aren't cut off by the newest-N cap (API max 1000).
  const { data, isLoading, error } = useTrades(prefs.days > 14 ? 1000 : 500, "filled");
  const { data: accounts } = useBrokerAccounts();

  const byId = useMemo(
    () => new Map((accounts ?? []).map((a) => [a.id, a])),
    [accounts]
  );
  const series = useMemo(() => {
    if (!data) return [];
    return buildSeries(data, byId, prefs.days, prefs.scope);
  }, [data, byId, prefs.days, prefs.scope]);

  const totalCum = series.length > 0 ? series[series.length - 1].cumulative : 0;
  const lineColor = totalCum < 0 ? NEG : POS;
  // No realised P&L anywhere (incl. the pre-window seed) → empty state.
  const hasRealised = series.length > 0 && (totalCum !== 0 || series.some((p) => p.realized !== 0));

  const set = (patch: Partial<PnlPrefs>) => setPrefs((p) => ({ ...p, ...patch }));

  return (
    <div className="widget widget-wide" data-testid="pnl-chart">
      <h3>
        P&amp;L (last {prefs.days} days){" "}
        <span className={`mono ${totalCum > 0 ? "pnl-pos" : totalCum < 0 ? "pnl-neg" : ""}`}>
          realised {rupees(totalCum)}
        </span>
      </h3>
      <div className="pnl-controls" data-testid="pnl-controls">
        <div className="chart-group" role="group" aria-label="account scope">
          {(["all", "paper", "live"] as const).map((s) => (
            <button
              key={s}
              type="button"
              className={`chart-btn${prefs.scope === s ? " on" : ""}`}
              onClick={() => set({ scope: s })}
            >
              {s === "all" ? "All" : s === "paper" ? "Paper" : "Live"}
            </button>
          ))}
        </div>
        <div className="chart-group" role="group" aria-label="time range">
          {RANGES.map((d) => (
            <button
              key={d}
              type="button"
              className={`chart-btn${prefs.days === d ? " on" : ""}`}
              onClick={() => set({ days: d })}
            >
              {d}D
            </button>
          ))}
        </div>
        <div className="chart-group" role="group" aria-label="chart view">
          {(["cumulative", "daily"] as const).map((v) => (
            <button
              key={v}
              type="button"
              className={`chart-btn${prefs.view === v ? " on" : ""}`}
              onClick={() => set({ view: v })}
            >
              {v === "cumulative" ? "Cumulative" : "Daily"}
            </button>
          ))}
        </div>
      </div>
      {isLoading ? (
        <Skeleton height={240} />
      ) : error ? (
        <p className="empty">
          {error instanceof ApiClientError && error.status === 404
            ? "Trades endpoint not available yet — backend doesn't expose /api/trades."
            : `Failed to load trades: ${error.message}`}
        </p>
      ) : !hasRealised ? (
        <p className="empty">
          {prefs.scope === "all"
            ? "No realised P&L yet."
            : `No realised ${prefs.scope} P&L yet.`}
        </p>
      ) : (
        <div style={{ width: "100%", height: 240 }}>
          <ResponsiveContainer>
            {prefs.view === "cumulative" ? (
              <AreaChart data={series} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={lineColor} stopOpacity={0.5} />
                    <stop offset="100%" stopColor={lineColor} stopOpacity={0.05} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#30363d" strokeDasharray="3 3" />
                <XAxis
                  dataKey="date"
                  tickFormatter={shortDate}
                  stroke="#8b949e"
                  fontSize={11}
                  minTickGap={24}
                />
                <YAxis stroke="#8b949e" fontSize={11} width={60} />
                <Tooltip
                  contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                  labelFormatter={(v: string) => shortDate(v)}
                  formatter={(v: number) => [rupees(v), "P&L"]}
                />
                <Area type="monotone" dataKey="cumulative" stroke={lineColor} fill="url(#pnlFill)" />
              </AreaChart>
            ) : (
              <BarChart data={series} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="#30363d" strokeDasharray="3 3" />
                <XAxis
                  dataKey="date"
                  tickFormatter={shortDate}
                  stroke="#8b949e"
                  fontSize={11}
                  minTickGap={24}
                />
                <YAxis stroke="#8b949e" fontSize={11} width={60} />
                <Tooltip
                  contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                  labelFormatter={(v: string) => shortDate(v)}
                  formatter={(v: number) => [rupees(v), "Day P&L"]}
                  cursor={{ fill: "rgba(139, 148, 158, 0.08)" }}
                />
                <Bar dataKey="realized" radius={[2, 2, 0, 0]}>
                  {series.map((p) => (
                    <Cell key={p.date} fill={p.realized < 0 ? NEG : POS} />
                  ))}
                </Bar>
              </BarChart>
            )}
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
