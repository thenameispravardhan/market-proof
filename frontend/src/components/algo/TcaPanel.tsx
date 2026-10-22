// Transaction-cost analysis: LIVE fills vs the quotes the runner decided at.
// Measured slippage above what the backtests assume = the backtests flatter the strategy.
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";

type TcaRow = { strategy_id: number; name: string; kind: string; fills: number; median_pct: number; mean_pct: number;
  rupees: number; assumed_pct: number | null };

export function TcaPanel({ shown }: { shown: (id: number) => boolean }) {
  const [days, setDays] = useState(30);
  const q = useQuery({ queryKey: ["algo", "tca", days], queryFn: () => api.get<{ rows: TcaRow[] }>(`/api/algo/tca?days=${days}`), refetchInterval: 60000 });
  const rows = (q.data?.rows ?? []).filter((r) => shown(r.strategy_id));
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }} data-testid="algo-tca">
      <h3>Fills vs quotes <span className="meta">live orders only — + = the fill cost you vs the price the runner decided at</span></h3>
      <label className="meta">last <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
        {[7, 30, 90, 365].map((d) => <option key={d} value={d}>{d} days</option>)}
      </select></label>
      {!rows.length ? <div className="empty">no live fills yet</div> : (
        <table style={{ marginTop: 6 }}>
          <thead><tr><th>Strategy</th><th>Kind</th><th>Fills</th><th>Median</th><th>Mean</th><th>Cost ₹</th><th title="the strategy's costs.slippage_pct — what its backtests charge per fill">Backtest assumes</th></tr></thead>
          <tbody>{rows.map((r) => {
            const worse = r.assumed_pct != null && r.median_pct > r.assumed_pct;
            return (
              <tr key={`${r.strategy_id}-${r.kind}`}>
                <td>{r.name}</td><td>{r.kind}</td><td className="mono">{r.fills}</td>
                <td className={`mono ${worse ? "pnl-neg" : ""}`} title={worse ? "measured above what the backtests assume — raise slippage % in the strategy" : undefined}>{r.median_pct.toFixed(3)}%</td>
                <td className="mono">{r.mean_pct.toFixed(3)}%</td><td className="mono">₹{Math.round(r.rupees).toLocaleString("en-IN")}</td>
                <td className="mono">{r.assumed_pct == null ? "—" : `${r.assumed_pct}%`}</td>
              </tr>
            );
          })}</tbody>
        </table>
      )}
    </div>
  );
}
