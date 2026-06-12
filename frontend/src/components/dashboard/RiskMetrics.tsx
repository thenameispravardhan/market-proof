// Risk metrics: small grid of the three most-watched numbers
//   - hard rules count (10 by default, from /api/dashboard/summary or
//     derived from the rules engine when the endpoint is missing)
//   - today's open positions
//   - today's realised P&L
//
// We compute today's realised P&L from /api/trades (sum of pnl where
// executed_at falls on today's UTC date) since the dashboard summary
// endpoint may not be exposed.

import { useMemo } from "react";
import { usePositions, useTrades, useDashboardSummary } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { Skeleton } from "./Skeleton";

const FALLBACK_HARD_RULES = 10;

function ymd(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "pos" | "neg" | "neutral";
}) {
  const cls = tone === "pos" ? "pnl-pos" : tone === "neg" ? "pnl-neg" : "";
  return (
    <div
      style={{
        flex: 1,
        minWidth: 120,
        padding: 10,
        background: "var(--bg)",
        border: "1px solid var(--border)",
        borderRadius: 6,
      }}
    >
      <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: "var(--text-dim)" }}>
        {label}
      </div>
      <div className={`mono ${cls}`} style={{ fontSize: 20, fontWeight: 600, marginTop: 4 }}>
        {value}
      </div>
    </div>
  );
}

export function RiskMetrics() {
  const positionsQ = usePositions();
  const tradesQ = useTrades(500);
  const summaryQ = useDashboardSummary();

  const todaysPnl = useMemo(() => {
    if (!tradesQ.data) return null;
    const today = ymd(new Date());
    let total = 0;
    let any = false;
    for (const t of tradesQ.data) {
      const ts = t.executed_at ?? t.created_at;
      if (!ts) continue;
      if (ymd(new Date(ts)) !== today) continue;
      any = true;
      total += t.pnl ?? 0;
    }
    return any ? total : null;
  }, [tradesQ.data]);

  const summary = summaryQ.data;
  const summaryMissing =
    summaryQ.error instanceof ApiClientError && summaryQ.error.status === 404;

  const isLoading = positionsQ.isLoading && !positionsQ.data && tradesQ.isLoading && !tradesQ.data;
  if (isLoading) {
    return (
      <div className="widget widget-wide" data-testid="risk-metrics">
        <h3>Risk Metrics</h3>
        <Skeleton height={80} />
      </div>
    );
  }

  const hardRules = summary?.hard_rules_count ?? FALLBACK_HARD_RULES;
  const openPositions = summary?.open_positions ?? positionsQ.data?.length ?? 0;
  const realizedPnl = summary?.todays_realized_pnl ?? todaysPnl ?? 0;
  const realizedTone: "pos" | "neg" | "neutral" =
    realizedPnl > 0 ? "pos" : realizedPnl < 0 ? "neg" : "neutral";

  return (
    <div className="widget widget-wide" data-testid="risk-metrics">
      <h3>Risk Metrics {summaryMissing && <span className="badge warn" title="Server-side summary endpoint missing; showing client-side fallback">client-side</span>}</h3>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <Metric label="Hard rules" value={String(hardRules)} />
        <Metric label="Open positions" value={String(openPositions)} />
        <Metric
          label="Realised P&L (today)"
          value={`₹${realizedPnl.toLocaleString(undefined, { maximumFractionDigits: 2 })}`}
          tone={realizedTone}
        />
      </div>
    </div>
  );
}
