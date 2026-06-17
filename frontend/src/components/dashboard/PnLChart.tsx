// PnL chart: cumulative realised P&L over the last N trading days.
//
// Realised P&L is reconstructed from /api/trades with the SAME FIFO
// round-trip logic the Trade History page uses (`buildRoundTrips`), so
// the two views always agree — they used to diverge because the chart
// summed raw per-trade pnl over every trade (incl. open entry legs and
// non-filled rows) while Trade History paired legs into round-trips.
//
// The per-day bars cover the last 14 days; the cumulative line is
// seeded with realised P&L from trades closed *before* that window, so
// the final cumulative value equals the all-time realised total shown
// on Trade History.

import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
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

function ymd(d: Date): string {
  return d.toISOString().slice(0, 10);
}

// Build the daily realised-P&L series from FIFO round-trips. Each
// closed round-trip's realised P&L is bucketed on its exit date. The
// cumulative line carries forward all realised P&L — including trades
// closed before the visible window — so its last point matches the
// Trade History realised total.
function buildSeries(trades: Trade[], byId: Map<number, BrokerAccount>, days = 14): PnlPoint[] {
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

export function PnLChart() {
  // Filled-only + the same 500-row window as Trade History so both
  // reconstruct the identical set of round-trips.
  const { data, isLoading, error } = useTrades(500, "filled");
  const { data: accounts } = useBrokerAccounts();

  const byId = useMemo(
    () => new Map((accounts ?? []).map((a) => [a.id, a])),
    [accounts]
  );
  const series = useMemo(() => {
    if (!data) return [];
    return buildSeries(data, byId, 14);
  }, [data, byId]);

  const totalCum = series.length > 0 ? series[series.length - 1].cumulative : 0;
  // No realised P&L anywhere (incl. the pre-window seed) → empty state.
  const hasRealised = series.length > 0 && (totalCum !== 0 || series.some((p) => p.realized !== 0));

  return (
    <div className="widget widget-wide" data-testid="pnl-chart">
      <h3>
        P&amp;L (last 14 days){" "}
        <span className={`mono ${totalCum > 0 ? "pnl-pos" : totalCum < 0 ? "pnl-neg" : ""}`}>
          realised ₹{totalCum.toLocaleString(undefined, { maximumFractionDigits: 2 })}
        </span>
      </h3>
      {isLoading ? (
        <Skeleton height={240} />
      ) : error ? (
        <p className="empty">
          {error instanceof ApiClientError && error.status === 404
            ? "Trades endpoint not available yet — backend doesn't expose /api/trades."
            : `Failed to load trades: ${error.message}`}
        </p>
      ) : !hasRealised ? (
        <p className="empty">No realised P&amp;L yet.</p>
      ) : (
        <div style={{ width: "100%", height: 240 }}>
          <ResponsiveContainer>
            <AreaChart data={series} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#3fb950" stopOpacity={0.5} />
                  <stop offset="100%" stopColor="#3fb950" stopOpacity={0.05} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#30363d" strokeDasharray="3 3" />
              <XAxis dataKey="date" tickFormatter={shortDate} stroke="#8b949e" fontSize={11} />
              <YAxis stroke="#8b949e" fontSize={11} width={60} />
              <Tooltip
                contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                labelFormatter={(v: string) => shortDate(v)}
                formatter={(v: number) => [`₹${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`, "P&L"]}
              />
              <Area type="monotone" dataKey="cumulative" stroke="#3fb950" fill="url(#pnlFill)" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
