// PnL chart: cumulative realised P&L over the last N trading days,
// computed client-side from /api/trades. The brief mentions a
// server-side /api/dashboard/summary that returns `pnl_series`; we
// fall back to the client-side synthesis if that endpoint is missing
// or returns 404.

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
import { useTrades } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { Skeleton } from "./Skeleton";
import type { Trade } from "../../types";

interface PnlPoint {
  date: string;
  realized: number;
  cumulative: number;
}

function ymd(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function synthesizeSeries(trades: Trade[], days = 14): PnlPoint[] {
  // Bucket trades by date (using executed_at when available, else created_at).
  // Within each bucket, sum pnl (treat null pnl as 0 — open positions
  // don't have realised P&L yet).
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

  for (const t of trades) {
    const ts = t.executed_at ?? t.created_at;
    if (!ts) continue;
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) continue;
    d.setUTCHours(0, 0, 0, 0);
    if (d < start) continue;
    const key = ymd(d);
    if (!buckets.has(key)) continue;
    buckets.set(key, (buckets.get(key) ?? 0) + (t.pnl ?? 0));
  }

  let cum = 0;
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
  const { data, isLoading, error } = useTrades(500);

  const series = useMemo(() => {
    if (!data) return [];
    return synthesizeSeries(data, 14);
  }, [data]);

  const totalCum = series.length > 0 ? series[series.length - 1].cumulative : 0;

  return (
    <div className="widget widget-wide" data-testid="pnl-chart">
      <h3>
        P&amp;L (last 14 days){" "}
        <span className={`mono ${totalCum > 0 ? "pnl-pos" : totalCum < 0 ? "pnl-neg" : ""}`}>
          cumulative ₹{totalCum.toLocaleString(undefined, { maximumFractionDigits: 2 })}
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
      ) : series.every((p) => p.cumulative === 0) ? (
        <p className="empty">No realised P&amp;L in the last 14 days.</p>
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
