// TCA — live fills vs the quotes the runner decided at (GET /api/algo/tca).
// Measured slippage per strategy and per instrument kind, next to the
// costs.slippage_pct its backtests assume. Read-only: it never edits a spec.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";

export type TcaStats = { fills: number; median_pct: number; mean_pct: number; median_rs: number; mean_rs: number; total_rs: number };
export type TcaStrategy = TcaStats & { strategy_id: number; name: string; assumed_pct: number; kinds: (TcaStats & { kind: string })[] };
export type TcaResult = { days: number; strategies: TcaStrategy[] };

const pct = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(3)}%`;
const rs = (v: number) => `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

/** "above" when the measured median slippage costs more than the backtest assumes. */
export function slippageVerdict(measuredPct: number, assumedPct: number): "above" | "within" {
  return measuredPct > assumedPct + 1e-9 ? "above" : "within";
}

export function TcaPanel() {
  const [days, setDays] = useState(30);
  const q = useQuery({ queryKey: ["algo", "tca", days], queryFn: () => api.get<TcaResult>(`/api/algo/tca?days=${days}`), refetchInterval: 60000 });
  const rows = q.data?.strategies ?? [];
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }} data-testid="algo-tca-panel">
      <h3>Fills vs quotes (TCA) <span className="meta">— LIVE fills only; + = the fill cost you against the quote the runner decided at</span>{" "}
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[7, 30, 90, 365].map((d) => <option key={d} value={d}>last {d} days</option>)}
        </select>
      </h3>
      {!rows.length ? <div className="empty">no live fills with a quote yet (paper fills fill at the quote and are left out)</div> : (
        <table>
          <thead><tr><th>Strategy</th><th>Kind</th><th>Fills</th><th>Median</th><th>Mean</th><th>Median ₹ / fill</th><th>Total ₹</th><th title="costs.slippage_pct in the strategy's spec — what its backtests charge per fill">Backtest assumes</th></tr></thead>
          <tbody>{rows.flatMap((s) => [s, ...s.kinds.map((k) => ({ ...k, sub: true as const }))].map((r, i) => {
            const sub = "sub" in r;
            const verdict = slippageVerdict(r.median_pct, s.assumed_pct);
            return (
              <tr key={`${s.strategy_id}-${i}`}>
                <td>{sub ? "" : s.name}</td>
                <td className="meta">{sub ? (r as { kind: string }).kind : "all"}</td>
                <td className="mono">{r.fills}</td>
                <td className={`mono ${verdict === "above" ? "pnl-neg" : ""}`} title={verdict === "above" ? "costs more than the backtest assumes" : "within the backtest's assumption"}>{pct(r.median_pct)}</td>
                <td className="mono">{pct(r.mean_pct)}</td>
                <td className="mono">{rs(r.median_rs)}</td>
                <td className="mono">{rs(r.total_rs)}</td>
                <td className="mono">{sub ? "" : `${s.assumed_pct}%`}</td>
              </tr>);
          }))}
          </tbody>
        </table>
      )}
    </div>
  );
}
