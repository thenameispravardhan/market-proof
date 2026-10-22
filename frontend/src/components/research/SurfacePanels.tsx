/**
 * B4: the recorded IV surface against flat IV, and tick replay of a
 * backtest's exits (app/research/surface.py, app/research/tick_replay.py).
 */
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

type Acc = { mae: number | null; mape_pct: number | null; inside_pct: number | null };
type Eval = {
  name: string; days: number; quotes: number; lag: number; methods: string[]; min_days: number; beats_flat: boolean; verdict: string;
  overall: Record<string, Acc>; vs: Record<string, { days_won: number; mean_edge: number | null; ci95: [number, number] | null }>;
  flat_source: string | null; elapsed_s: number;
};
type Replay = {
  replayable: boolean; why?: string; trades: number; replayed?: number; no_ticks?: number; agree?: number; agree_pct?: number | null;
  pnl_diff?: number; ambiguous_candles?: number; ambiguous_right?: number; flipped?: Record<string, number>;
  notes?: { symbol: string; note: string }[];
};

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${res.status}`);
  return j as T;
}

const LABEL: Record<string, string> = { surface: "recorded surface", flat_atm: "flat — the same snapshot's ATM IV", flat: "flat — India VIX (today's index backtests)" };
const num = (v: number | null | undefined, dp = 2) => (v === null || v === undefined ? "—" : v.toFixed(dp));

export function SurfaceCheck({ range }: { range: { start: string; end: string } }) {
  const [name, setName] = useState("NIFTY");
  const [lag, setLag] = useState(1);
  const run = useMutation({ mutationFn: () => post<Eval>("/api/algo/research/surface/evaluate", { name, ...range, lag }) });
  const r = run.data;
  return (
    <div className="widget widget-wide" data-testid="surface-check" style={{ marginBottom: 12 }}>
      <h3>IV surface vs flat IV <span className="meta">recorded option chains (Data → Research recording), {range.start} → {range.end}</span></h3>
      <div className="algo-operand" style={{ gap: 10 }}>
        <label className="meta">chain <input value={name} onChange={(e) => setName(e.target.value.toUpperCase())} style={{ width: 110 }} /></label>
        <label className="meta" title="each snapshot is priced with the surface fitted this many snapshots earlier — what a backtest at that moment could have known">fitted <input type="number" min={1} max={60} value={lag} onChange={(e) => setLag(Math.max(1, Math.min(60, Number(e.target.value) || 1)))} style={{ width: 48 }} /> snapshots earlier</label>
        <button type="button" className="primary" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Checking…" : "Check against flat IV"}</button>
        <span className="meta">The surface goes to live Greeks only once it beats flat IV here, on at least 10 recorded days.</span>
      </div>
      {run.error && <div className="pnl-neg">{(run.error as Error).message}</div>}
      {r && (
        <>
          <div style={{ margin: "6px 0" }} data-testid="surface-verdict">
            <span className={`badge ${r.beats_flat ? "positive" : r.days < r.min_days ? "neutral" : "negative"}`}>{r.verdict}</span>{" "}
            <span className="meta">{r.days} days · {r.quotes.toLocaleString("en-IN")} quotes priced · {r.elapsed_s}s</span>
          </div>
          <table>
            <thead><tr><th>Priced with</th><th title="mean absolute error against the quote's mid">MAE ₹</th><th>MAE % of premium</th><th>inside bid-ask</th><th title="days it priced closer than the surface did / the surface's edge per day, with a 95% bootstrap CI">vs the surface</th></tr></thead>
            <tbody>{r.methods.map((m) => (
              <tr key={m}>
                <td>{LABEL[m] ?? m}</td>
                <td className="mono">{num(r.overall[m]?.mae)}</td><td className="mono">{num(r.overall[m]?.mape_pct)}%</td><td className="mono">{num(r.overall[m]?.inside_pct, 1)}%</td>
                <td className="meta">{m === "surface" ? "—" : r.vs[m] ? `surface closer on ${r.vs[m].days_won}/${r.days} days, ₹${num(r.vs[m].mean_edge)} a quote${r.vs[m].ci95 ? ` (95% CI ${num(r.vs[m].ci95![0])} to ${num(r.vs[m].ci95![1])})` : ""}` : "—"}</td>
              </tr>))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

export function TickReplay({ spec, range }: { spec: unknown; range: { start: string; end: string } }) {
  const run = useMutation({ mutationFn: () => post<Replay>("/api/algo/research/tick-replay", { spec, ...range }) });
  const r = run.data;
  return (
    <div className="widget widget-wide" data-testid="tick-replay" style={{ marginBottom: 12 }}>
      <h3>Tick replay of exits <span className="meta">the builder's strategy, {range.start} → {range.end}, against recorded trade prints</span></h3>
      <div className="algo-operand" style={{ gap: 10 }}>
        <button type="button" className="primary" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Replaying…" : "Replay exits on ticks"}</button>
        <span className="meta">A candle doesn't say whether its high or its low came first: where a stop and a target sit inside one candle the backtest guesses. The ticks don't.</span>
      </div>
      {run.error && <div className="pnl-neg">{(run.error as Error).message}</div>}
      {r && !r.replayable && <div className="meta" data-testid="replay-summary">Not replayed: {r.why}.</div>}
      {r && r.replayable && (
        <div className="meta" data-testid="replay-summary" style={{ marginTop: 6 }}>
          {r.replayed} of {r.trades} trades replayed ({r.no_ticks} without recorded ticks). <b>{num(r.agree_pct, 1)}% exited as the backtest said</b>;
          {" "}{Object.entries(r.flipped ?? {}).map(([k, n]) => `${n} ${k.replace("->", " → ")}`).join(", ") || "none reversed"}.
          {" "}Stop and target in one candle: {r.ambiguous_candles}, the backtest's guess right on {r.ambiguous_right}.
          {" "}P&L that moves: <span className={r.pnl_diff! < 0 ? "pnl-neg" : r.pnl_diff! > 0 ? "pnl-pos" : ""}>₹{Math.round(r.pnl_diff ?? 0).toLocaleString("en-IN")}</span>.
        </div>
      )}
    </div>
  );
}
