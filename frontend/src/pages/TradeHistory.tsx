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

function _acctMeta(
  t: Trade | undefined,
  byId: Map<number, BrokerAccount>
): { isPaper: boolean; accountName: string } {
  const acct = t?.broker_account_id != null ? byId.get(t.broker_account_id) : undefined;
  const isPaper = acct
    ? acct.paper_mode
    : (t?.broker_order_id?.startsWith("PAPER-") ?? true);
  const accountName = acct && !acct.paper_mode ? acct.name : isPaper ? "Paper" : "Fyers";
  return { isPaper, accountName };
}

// Pair entry + exit legs into round-trips via FIFO, keyed on SYMBOL alone.
// We can't key on signal_id or account: auto-pipeline exit legs (the SELL
// the trade manager issues) carry neither the original signal_id nor the
// account, so symbol is the only reliable join. A BUY opens a lot; a later
// opposite-side SELL closes it (and vice-versa for shorts). Leftover lots
// stay OPEN.
function buildRoundTrips(
  trades: Trade[],
  byId: Map<number, BrokerAccount>
): RoundTrip[] {
  const filled = trades.filter((t) => t.status === "filled");
  const groups = new Map<string, Trade[]>();
  for (const t of filled) {
    (groups.get(t.symbol) ?? groups.set(t.symbol, []).get(t.symbol)!).push(t);
  }
  const out: RoundTrip[] = [];
  for (const [, legs] of groups) {
    legs.sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""));
    const openLots: { side: string; qty: number; price: number; t: Trade }[] = [];
    let seq = 0;
    for (const t of legs) {
      let qty = Math.abs(t.quantity || 0);
      const exitQtyTotal = Math.abs(t.quantity || 0) || 1;
      while (qty > 0 && openLots.length > 0 && openLots[0].side !== t.side) {
        const lot = openLots[0];
        const matched = Math.min(qty, lot.qty);
        const long = lot.side === "BUY";
        const entryP = lot.price;
        const exitP = t.price ?? 0;
        const { isPaper, accountName } = _acctMeta(lot.t, byId);
        // Prefer the broker's realised P&L on the exit leg (scaled to the
        // matched quantity); fall back to entry/exit price math.
        const pnl =
          t.pnl != null
            ? (t.pnl * matched) / exitQtyTotal
            : (long ? exitP - entryP : entryP - exitP) * matched;
        out.push({
          key: `r-${lot.t.id}-${t.id}-${seq++}`,
          symbol: t.symbol,
          isPaper,
          accountName,
          side: lot.side,
          quantity: matched,
          entryPrice: entryP,
          exitPrice: exitP,
          exitTime: t.executed_at ?? t.created_at ?? null,
          pnl,
          open: false,
        });
        lot.qty -= matched;
        qty -= matched;
        if (lot.qty === 0) openLots.shift();
      }
      if (qty > 0) openLots.push({ side: t.side, qty, price: t.price ?? 0, t });
    }
    for (const lot of openLots) {
      const { isPaper, accountName } = _acctMeta(lot.t, byId);
      out.push({
        key: `o-${lot.t.id}`,
        symbol: lot.t.symbol,
        isPaper,
        accountName,
        side: lot.side,
        quantity: lot.qty,
        entryPrice: lot.price,
        exitPrice: null,
        exitTime: lot.t.executed_at ?? lot.t.created_at ?? null,
        pnl: null,
        open: true,
      });
    }
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
  // Filled-only: a burst of rejected HOLD signals must not push real fills
  // (especially older entry legs) out of the newest-N window, or a closed
  // round-trip would lose its entry and show as a phantom open position.
  const { data: trades, isLoading, error } = useTrades(500, "filled");
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
