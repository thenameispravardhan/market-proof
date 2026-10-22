// The book: every automation's open positions together — exposure per underlying,
// day P&L and margin against the book limits (Settings → Circuit breakers), and the
// Algo Lab kill switch. GET /api/algo/book, POST /api/algo/book/kill.
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";

type Under = { symbol: string; positions: number; margin: number; mtm: number | null; delta: number | null };
export type Book = {
  underlyings: Under[]; positions: number; margin: number; realized_today: number; open_mtm: number | null;
  day_pnl: number | null; limits: { max_day_loss: number | null; max_margin: number | null }; halted: boolean;
};

const rs = (v: number | null | undefined) => (v == null ? "—" : `₹${Math.round(v).toLocaleString("en-IN")}`);
const cls = (v: number | null | undefined) => (v == null ? "" : v >= 0 ? "pnl-pos" : "pnl-neg");

export function BookPanel() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["algo", "book"], queryFn: () => api.get<Book>("/api/algo/book"), refetchInterval: 5000 });
  const [err, setErr] = useState<string | null>(null);
  const b = q.data;
  if (!b?.underlyings) return null;
  const kill = async () => {
    if (!window.confirm(`Flatten ALL ${b.positions} automation position(s) and stop every automation's entries until tomorrow? (The news bot and the Trade page are not affected.)`)) return;
    setErr(null);
    try { await api.post("/api/algo/book/kill", {}); } catch (e) { setErr(String((e as Error).message ?? e)); }
    qc.invalidateQueries({ queryKey: ["algo"] });
  };
  const loss = b.limits.max_day_loss, cap = b.limits.max_margin;
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }} data-testid="algo-book">
      <h3>Book <span className="meta">every automation together — limits in Settings → Circuit breakers</span>
        {b.halted && <span className="badge sell" style={{ marginLeft: 8 }} data-testid="algo-book-halted">HALTED TODAY</span>}</h3>
      <div className="algo-operand" style={{ gap: 16 }}>
        <span className="meta">day P&amp;L <b className={cls(b.day_pnl)}>{rs(b.day_pnl)}</b>{loss ? <> of −{rs(loss)} limit</> : null}</span>
        <span className="meta">realised {rs(b.realized_today)} · open {rs(b.open_mtm)}</span>
        <span className="meta">margin <b>{rs(b.margin)}</b>{cap ? <> of {rs(cap)} cap</> : null}</span>
        <button type="button" className="btn-sm danger" disabled={!b.positions && b.halted} onClick={kill}
          title="flatten every automation's open position and stop all automation entries for today">Kill all automations</button>
      </div>
      {err && <div className="pnl-neg">{err}</div>}
      {b.underlyings.length > 0 && (
        <table style={{ marginTop: 6 }}>
          <thead><tr><th>Underlying</th><th>Positions</th><th title="net delta in lots (shares for cash): + = long the underlying">Net Δ</th><th>Margin</th><th>MTM</th></tr></thead>
          <tbody>{b.underlyings.map((u) => (
            <tr key={u.symbol}><td>{u.symbol.replace(/^NSE:/, "")}</td><td className="mono">{u.positions}</td>
              <td className="mono">{u.delta == null ? "—" : u.delta.toFixed(2)}</td><td className="mono">{rs(u.margin)}</td>
              <td className={`mono ${cls(u.mtm)}`}>{rs(u.mtm)}</td></tr>))}
          </tbody>
        </table>
      )}
    </div>
  );
}
