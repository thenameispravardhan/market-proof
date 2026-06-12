// RunDetail — shows metrics, trades table, and equity curve for a run.
import { useBacktestRun, useBacktestEquityCurve, useBacktestTrades } from "../../hooks/useApi";
import { EquityCurveChart } from "./EquityCurveChart";

interface Props {
  runId: number | null;
}

function fmt(v: unknown, decimals = 2): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return isFinite(v) ? v.toFixed(decimals) : "—";
  return String(v);
}

export function RunDetail({ runId }: Props) {
  const { data: run, isLoading: runLoading } = useBacktestRun(runId);
  const { data: equityCurveData } = useBacktestEquityCurve(runId);
  const { data: tradesData } = useBacktestTrades(runId);

  if (!runId) {
    return (
      <div className="widget" data-testid="run-detail">
        <h3>Run Detail</h3>
        <p className="empty">Select a run or click "New Run" to get started.</p>
      </div>
    );
  }

  if (runLoading) {
    return (
      <div className="widget" data-testid="run-detail">
        <h3>Run Detail</h3>
        <p className="empty">Loading…</p>
      </div>
    );
  }

  if (!run) {
    return (
      <div className="widget" data-testid="run-detail">
        <h3>Run Detail</h3>
        <p className="pnl-neg">Run not found.</p>
      </div>
    );
  }

  const m = run.metrics ?? {};
  const curve = equityCurveData?.curve ?? [];
  const trades = (tradesData?.trades ?? []) as Array<Record<string, unknown>>;

  return (
    <div className="widget" data-testid="run-detail">
      <h3>
        {run.name ?? `Run #${run.id}`}{" "}
        <span className={`badge ${run.status === "done" ? "info" : run.status === "failed" ? "danger" : "warn"}`}>
          {run.status}
        </span>
      </h3>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8, marginBottom: 16 }}>
        {[
          ["Total return", fmt(m.total_return_pct) + "%"],
          ["Sharpe", fmt(m.sharpe)],
          ["Max drawdown", fmt(m.max_drawdown_pct) + "%"],
          ["Win rate", fmt(m.win_rate_pct) + "%"],
          ["Profit factor", fmt(m.profit_factor)],
          ["Total trades", String(m.total_trades ?? trades.length)],
        ].map(([label, val]) => (
          <div key={label} style={{ background: "#0d1117", padding: 8, borderRadius: 4 }}>
            <div style={{ color: "var(--text-dim)", fontSize: 11 }}>{label}</div>
            <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{val}</div>
          </div>
        ))}
      </div>

      {curve.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <h4>Equity Curve</h4>
          <EquityCurveChart data={curve} initialCapital={run.initial_capital} />
        </div>
      )}

      {run.status === "running" && (
        <p className="empty" style={{ color: "var(--amber)" }}>⏳ Backtest running… refreshing every 2s</p>
      )}
      {run.status === "pending" && (
        <p className="empty">⏳ Backtest queued…</p>
      )}
      {run.status === "failed" && (
        <p className="pnl-neg">Backtest failed. Check server logs.</p>
      )}

      {trades.length > 0 && (
        <div>
          <h4>Trades ({trades.length})</h4>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ color: "var(--text-dim)", borderBottom: "1px solid #30363d" }}>
                  <th style={{ textAlign: "left", padding: "4px 8px" }}>Symbol</th>
                  <th style={{ textAlign: "left", padding: "4px 8px" }}>Side</th>
                  <th style={{ textAlign: "right", padding: "4px 8px" }}>Qty</th>
                  <th style={{ textAlign: "right", padding: "4px 8px" }}>Price</th>
                  <th style={{ textAlign: "right", padding: "4px 8px" }}>P&L</th>
                  <th style={{ textAlign: "left", padding: "4px 8px" }}>Date</th>
                </tr>
              </thead>
              <tbody>
                {trades.slice(0, 50).map((t, i) => {
                  const pnl = Number(t.pnl ?? 0);
                  return (
                    <tr key={i} style={{ borderBottom: "1px solid #21262d" }}>
                      <td style={{ padding: "4px 8px" }} className="mono">{String(t.symbol ?? "—")}</td>
                      <td style={{ padding: "4px 8px" }}>
                        <span className={`badge ${t.side === "BUY" ? "info" : "danger"}`}>
                          {String(t.side ?? "—")}
                        </span>
                      </td>
                      <td style={{ padding: "4px 8px", textAlign: "right" }}>{String(t.quantity ?? "—")}</td>
                      <td style={{ padding: "4px 8px", textAlign: "right" }} className="mono">
                        ₹{Number(t.price ?? 0).toLocaleString()}
                      </td>
                      <td style={{ padding: "4px 8px", textAlign: "right" }} className={`mono ${pnl >= 0 ? "pnl-pos" : "pnl-neg"}`}>
                        {pnl >= 0 ? "+" : ""}₹{pnl.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                      </td>
                      <td style={{ padding: "4px 8px", fontSize: 11, color: "var(--text-dim)" }}>
                        {t.executed_at ? new Date(String(t.executed_at)).toLocaleDateString() : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {trades.length > 50 && (
              <p style={{ fontSize: 11, color: "var(--text-dim)", marginTop: 4 }}>
                Showing first 50 of {trades.length} trades.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
