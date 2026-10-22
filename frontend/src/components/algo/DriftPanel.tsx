// Live vs backtest drift: re-run the strategy over the days it traded and put
// each live / paper trade beside the backtest's. One engine runs both, so any
// gap is data, fills or timing — the check that "what you backtest is what runs".
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../api/client";

type Row = { symbol: string; side: string; entry_t: number; entry: number | null; exit_t: number | null; exit: number | null;
  reason: string | null; net: number | null; status?: string };
type Pair = { live: Row; bt: Row; entry_gap_pct: number | null; net_gap: number | null; same_reason: boolean };
type VersionDrift = { version: number; from: number; to: number; matched: Pair[]; live_only: Row[]; bt_only: Row[];
  live_net: number; bt_net: number; notes: { symbol: string; note: string }[] };
export type Drift = { strategy_id: number; days: number; versions: VersionDrift[]; note?: string };

const t = (s: number | null) => (s ? new Date(s * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "—");
const px = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(2));
const rs = (v: number | null | undefined) => (v == null ? "—" : `₹${Math.round(v).toLocaleString("en-IN")}`);
const cls = (v: number | null | undefined) => (v == null ? "" : v >= 0 ? "pnl-pos" : "pnl-neg");

function Rows({ rows, title }: { rows: Row[]; title: string }) {
  if (!rows.length) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <div className="meta">{title}</div>
      <table>
        <thead><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net</th></tr></thead>
        <tbody>{rows.map((r, i) => (
          <tr key={i}><td>{r.symbol.replace(/^NSE:/, "")}</td><td>{r.side === "BUY" ? "LONG" : "SHORT"}</td>
            <td className="mono">{px(r.entry)} <span className="meta">{t(r.entry_t)}</span></td>
            <td className="mono">{px(r.exit)} <span className="meta">{t(r.exit_t)}</span></td>
            <td>{r.status === "rejected" ? "rejected" : r.reason ?? "open"}</td><td className={`mono ${cls(r.net)}`}>{rs(r.net)}</td></tr>))}
        </tbody>
      </table>
    </div>
  );
}

export function DriftPanel({ strategies }: { strategies: { id: number; name: string }[] }) {
  const [sid, setSid] = useState<number | null>(null);
  const [days, setDays] = useState(10);
  const id = sid ?? strategies[0]?.id ?? null;
  const run = useMutation({ mutationFn: () => api.get<Drift>(`/api/algo/strategies/${id}/drift?days=${days}`) });
  if (!strategies.length) return null;
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }} data-testid="algo-drift">
      <h3>Live vs backtest <span className="meta">re-runs the strategy over the days it traded and lines up every trade</span></h3>
      <div className="algo-operand" style={{ gap: 10 }}>
        <select value={id ?? ""} onChange={(e) => setSid(Number(e.target.value))}>
          {strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <label className="meta">last <input type="number" min={1} max={60} value={days} style={{ width: 52 }}
          onChange={(e) => setDays(Math.max(1, Math.min(60, Math.floor(Number(e.target.value) || 1))))} /> days</label>
        <button type="button" className="btn-sm" disabled={!id || run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Backtesting…" : "Compare"}</button>
      </div>
      {run.error && <div className="pnl-neg">{String((run.error as Error).message ?? run.error)}</div>}
      {run.data?.note && <div className="meta" style={{ marginTop: 6 }}>{run.data.note}</div>}
      {run.data?.versions.map((v) => (
        <div key={v.version} style={{ marginTop: 10 }} data-testid={`algo-drift-v${v.version}`}>
          <div className="meta" style={{ color: "var(--text)" }}>
            v{v.version}: {v.matched.length} matched · {v.live_only.length} only live · {v.bt_only.length} only in the backtest ·
            net live <span className={cls(v.live_net)}>{rs(v.live_net)}</span> vs backtest <span className={cls(v.bt_net)}>{rs(v.bt_net)}</span>
          </div>
          {v.notes.map((n, i) => <div key={i} className="meta">{n.symbol}: {n.note}</div>)}
          {v.matched.length > 0 && (
            <table style={{ marginTop: 6 }}>
              <thead><tr><th>Symbol</th><th>Side</th><th>Entry live / test</th><th title="live entry vs the backtest's, in % — slippage and timing">Entry gap</th><th>Exit live / test</th><th>Reason live / test</th><th>Net live / test</th></tr></thead>
              <tbody>{v.matched.map((p, i) => (
                <tr key={i}><td>{p.live.symbol.replace(/^NSE:/, "")}</td><td>{p.live.side === "BUY" ? "LONG" : "SHORT"}</td>
                  <td className="mono">{px(p.live.entry)} / {px(p.bt.entry)} <span className="meta">{t(p.live.entry_t)}</span></td>
                  <td className="mono">{p.entry_gap_pct == null ? "—" : `${p.entry_gap_pct.toFixed(2)}%`}</td>
                  <td className="mono">{px(p.live.exit)} / {px(p.bt.exit)}</td>
                  <td className={p.same_reason ? "" : "pnl-neg"}>{p.live.status === "rejected" ? "rejected" : p.live.reason ?? "open"} / {p.bt.reason}</td>
                  <td className="mono"><span className={cls(p.live.net)}>{rs(p.live.net)}</span> / <span className={cls(p.bt.net)}>{rs(p.bt.net)}</span></td></tr>))}
              </tbody>
            </table>
          )}
          <Rows rows={v.live_only} title="Only live — the backtest didn't take these (a different candle, or a manual / external action)" />
          <Rows rows={v.bt_only} title="Only in the backtest — live missed these (runner off, no live price, rejected order, cap reached)" />
        </div>
      ))}
    </div>
  );
}
