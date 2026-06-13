// Trade History — executed trades as round-trips (entry + exit paired
// into one row), split into two sections: Fyers and Paper Trading.
//
// The entry leg (BUY, no realised P&L) and the exit leg (SELL carrying
// realised P&L) of the same position share a `signal_id`, so we group
// filled trades by signal_id to reconstruct each round-trip and show
// entry price, exit price, quantity and P&L on a single row. A position
// that hasn't been closed yet shows as "OPEN" with no exit/P&L.

import { useMemo } from "react";
import { useBrokerAccounts, useTrades } from "../hooks/useApi";
import type { BrokerAccount, Trade } from "../types";

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

interface RoundTrip {
  key: string;
  symbol: string;
  isPaper: boolean;
  accountName: string;
  side: string; // BUY (long) / SELL (short) — the opening side
  quantity: number;
  entryPrice: number | null;
  exitPrice: number | null;
  exitTime: string | null;
  pnl: number | null;
  open: boolean;
}

function buildRoundTrips(
  trades: Trade[],
  byId: Map<number, BrokerAccount>
): RoundTrip[] {
  const filled = trades.filter((t) => t.status === "filled");

  // Group the entry + exit legs that belong to the same position.
  const groups = new Map<string, Trade[]>();
  for (const t of filled) {
    const k = t.signal_id != null ? `sig-${t.signal_id}` : `trade-${t.id}`;
    const arr = groups.get(k);
    if (arr) arr.push(t);
    else groups.set(k, [t]);
  }

  const out: RoundTrip[] = [];
  for (const [k, legs] of groups) {
    // The exit leg carries realised P&L; the other is the entry.
    const exitLeg = legs.find((l) => l.pnl !== null && l.pnl !== undefined) ?? null;
    const entryLeg = legs.find((l) => l !== exitLeg) ?? legs[0];

    const acctId = entryLeg.broker_account_id ?? exitLeg?.broker_account_id ?? null;
    const acct = acctId != null ? byId.get(acctId) : undefined;
    const isPaper =
      acct ? acct.paper_mode : (entryLeg.broker_order_id?.startsWith("PAPER-") ?? true);
    const accountName = acct && !acct.paper_mode ? acct.name : isPaper ? "Paper" : "Fyers";

    out.push({
      key: k,
      symbol: entryLeg.symbol,
      isPaper,
      accountName,
      side: entryLeg.side,
      quantity: Math.abs(entryLeg.quantity || exitLeg?.quantity || 0),
      entryPrice: entryLeg.price || null,
      exitPrice: exitLeg ? exitLeg.price : null,
      exitTime: exitLeg?.executed_at ?? exitLeg?.created_at ?? null,
      pnl: exitLeg?.pnl ?? null,
      open: exitLeg == null,
    });
  }
  // Most recently closed first; open positions sink to the bottom.
  out.sort((a, b) => {
    if (a.open !== b.open) return a.open ? 1 : -1;
    return (b.exitTime ?? "").localeCompare(a.exitTime ?? "");
  });
  return out;
}

function Section({ title, rows }: { title: string; rows: RoundTrip[] }) {
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
                <th className="mono">P&amp;L</th>
                <th>Closed (IST)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
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
                  <td className={`mono ${pnlClass(r.pnl)}`}>
                    {r.open ? "—" : fmtMoney(r.pnl)}
                  </td>
                  <td className="mono">{r.open ? "—" : fmtTime(r.exitTime)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function TradeHistory() {
  const { data: trades, isLoading, error } = useTrades(500);
  const { data: accounts } = useBrokerAccounts();

  const byId = useMemo(
    () => new Map((accounts ?? []).map((a) => [a.id, a])),
    [accounts]
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
        <Section title="Fyers — Executed Trades" rows={fyers} />
        <Section title="Paper Trading — Executed Trades" rows={paper} />
      </div>
    </div>
  );
}
