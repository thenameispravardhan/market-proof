// Trade History — executed trades as round-trips (entry + exit paired
// into one row), split into two sections: Fyers and Paper Trading.
//
// Round-trips are reconstructed by `buildRoundTrips` (shared with the
// dashboard P&L chart so the two views agree): filled legs are paired
// FIFO per symbol to show entry price, exit price, quantity and P&L on
// a single row. A position that hasn't been closed yet shows as "OPEN"
// with no exit/P&L — its stop-loss / target is editable inline.

import { useMemo } from "react";
import { useBrokerAccounts, useManagedPositions, useTrades } from "../hooks/useApi";
import { LevelsCell } from "../components/positions/LevelsCell";
import { buildRoundTrips, type RoundTrip } from "../lib/roundtrips";
import type { ManagedPosition } from "../types";

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  let s = iso.trim();
  if (!/Z$|[+-]\d{2}:?\d{2}$/.test(s)) s += "Z";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

function pnlClass(v: number | null | undefined): string {
  if (!v) return "";
  return v > 0 ? "pnl-pos" : v < 0 ? "pnl-neg" : "";
}

function Section({
  title,
  rows,
  managedBy,
}: {
  title: string;
  rows: RoundTrip[];
  managedBy: Map<string, ManagedPosition>;
}) {
  const realised = rows.reduce((acc, r) => acc + (r.pnl ?? 0), 0);
  return (
    <div className="widget widget-wide" data-testid={`trades-${title.toLowerCase().includes("fyers") ? "fyers" : "paper"}`}>
      <h3>
        {title}
        <span className="mono" style={{ fontWeight: 400 }}>
          {rows.length} trade{rows.length === 1 ? "" : "s"} · P&amp;L{" "}
          <span className={pnlClass(realised)}>₹{fmtMoney(realised)}</span>
        </span>
      </h3>
      {rows.length === 0 ? (
        <p className="empty">No executed trades on this account yet.</p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th className="mono">Qty</th>
                <th className="mono">Entry</th>
                <th className="mono">Exit</th>
                <th className="mono">SL / Target</th>
                <th className="mono">P&amp;L</th>
                <th>Closed (IST)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const m = managedBy.get(r.symbol);
                return (
                  <tr key={r.key}>
                    <td className="mono">{r.symbol}</td>
                    <td>
                      <span className={`badge ${r.side === "BUY" ? "buy" : "sell"}`}>{r.side}</span>
                    </td>
                    <td className="mono">{r.quantity}</td>
                    <td className="mono">{fmtMoney(r.entryPrice)}</td>
                    <td className="mono">
                      {r.open ? <span className="badge warn">OPEN</span> : fmtMoney(r.exitPrice)}
                    </td>
                    <td className="mono">
                      {r.open ? (
                        <LevelsCell
                          symbol={r.symbol}
                          stopLoss={m?.stop_loss ?? null}
                          target={m?.target ?? null}
                        />
                      ) : (
                        <span className="text-dim">—</span>
                      )}
                    </td>
                    <td className={`mono ${pnlClass(r.pnl)}`}>
                      {r.open ? "—" : fmtMoney(r.pnl)}
                    </td>
                    <td className="mono">{r.open ? "—" : fmtTime(r.exitTime)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function TradeHistory() {
  // Filled-only: a burst of rejected HOLD signals must not push real fills
  // (especially older entry legs) out of the newest-N window, or a closed
  // round-trip would lose its entry and show as a phantom open position.
  const { data: trades, isLoading, error } = useTrades(500, "filled");
  const { data: accounts } = useBrokerAccounts();
  const { data: managed } = useManagedPositions();

  const byId = useMemo(
    () => new Map((accounts ?? []).map((a) => [a.id, a])),
    [accounts]
  );
  const managedBy = useMemo(
    () => new Map((managed ?? []).map((m) => [m.symbol, m])),
    [managed]
  );
  const roundtrips = useMemo(
    () => buildRoundTrips(trades ?? [], byId),
    [trades, byId]
  );
  const fyers = roundtrips.filter((r) => !r.isPaper);
  const paper = roundtrips.filter((r) => r.isPaper);

  const realised = roundtrips.reduce((a, r) => a + (r.pnl ?? 0), 0);
  const closed = roundtrips.filter((r) => !r.open && r.pnl !== null);
  const wins = closed.filter((r) => (r.pnl ?? 0) > 0).length;
  const winRate = closed.length > 0 ? (wins / closed.length) * 100 : null;

  if (isLoading) {
    return (
      <div>
        <h1 className="page-title">Trade History</h1>
        <p className="empty">Loading…</p>
      </div>
    );
  }
  if (error) {
    return (
      <div>
        <h1 className="page-title">Trade History</h1>
        <p className="empty">Failed to load trades: {(error as Error).message}</p>
      </div>
    );
  }

  return (
    <div>
      <h1 className="page-title">Trade History</h1>

      <div className="stat-row">
        <div className="stat">
          <div className="stat-label">Round-trips</div>
          <div className="stat-value">{roundtrips.length}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Realised P&amp;L</div>
          <div className={`stat-value ${pnlClass(realised)}`}>₹{fmtMoney(realised)}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Win rate</div>
          <div className="stat-value">
            {winRate === null ? "—" : `${winRate.toFixed(0)}%`}
          </div>
        </div>
      </div>

      <div className="dashboard-grid">
        <Section title="Fyers — Executed Trades" rows={fyers} managedBy={managedBy} />
        <Section title="Paper Trading — Executed Trades" rows={paper} managedBy={managedBy} />
      </div>
    </div>
  );
}
