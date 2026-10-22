/**
 * B2 pair finder: which symbols' prices move together and snap back
 * (app/research/pairs.py). A pair goes to the builder as a one-leg strategy —
 * trade the first symbol on its spread against the second — carrying every
 * pair scanned as tries on the idea.
 */
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

export type PairRow = {
  a: string; b: string; bars: number; beta: number; corr: number | null; adf_fit: number | null; adf_held_out: number | null;
  half_life: number | null; z_now: number | null; spread_sd_pct: number; cointegrated: boolean; holds_out: boolean; window: number;
  spec: Record<string, unknown>;
};
type Scan = { tested: number; skipped: number; cointegrated: number; confirmed: number; expected_by_luck: number; pairs: PairRow[];
  anchor: string | null; timeframe: number; days: number; notes: { symbol: string; note: string }[]; elapsed_s: number };

const short = (s: string) => s.replace(/^NSE:/, "").replace(/-EQ$/, "");
const num = (v: number | null | undefined, dp = 2) => (v === null || v === undefined ? "—" : v.toFixed(dp));

export function PairFinder({ timeframes, onUse }: { timeframes: number[]; onUse: (spec: Record<string, unknown>, name: string, tries: number) => void }) {
  const [symbols, setSymbols] = useState("HDFCBANK, ICICIBANK, KOTAKBANK, AXISBANK, SBIN");
  const [anchor, setAnchor] = useState("NSE:NIFTYBANK-INDEX");
  const [tf, setTf] = useState(60);
  const [days, setDays] = useState(365);
  const scan = useMutation({
    mutationFn: async () => {
      const res = await fetch("/api/algo/research/pairs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: symbols.split(/[\s,]+/).filter(Boolean), anchor: anchor.trim() || null, timeframe: tf, days }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
      return body as Scan;
    },
  });
  const r = scan.data;
  return (
    <div className="widget widget-wide" data-testid="pair-finder" style={{ marginBottom: 12 }}>
      <h3>Pair finder <span className="meta">stocks against an index, or peers against each other, whose spread snaps back</span></h3>
      <div className="algo-operand" style={{ gap: 10, flexWrap: "wrap" }}>
        <label className="meta">symbols <input value={symbols} onChange={(e) => setSymbols(e.target.value)} style={{ width: 340 }} data-testid="pair-symbols" /></label>
        <label className="meta" title="each symbol against this one (stock vs index); empty = every pair of the symbols (peer vs peer)">against <input value={anchor} onChange={(e) => setAnchor(e.target.value)} placeholder="empty = peers" style={{ width: 170 }} data-testid="pair-anchor" /></label>
        <label className="meta" title="the timeframe the strategy will trade: half-life and window are in its bars">timeframe <select value={tf} onChange={(e) => setTf(Number(e.target.value))}>{timeframes.map((t) => <option key={t} value={t}>{t}m</option>)}</select></label>
        <label className="meta">last <input type="number" value={days} min={30} max={1098} onChange={(e) => setDays(Number(e.target.value) || 365)} style={{ width: 60 }} /> days</label>
        <button type="button" className="primary" disabled={scan.isPending} onClick={() => scan.mutate()}>{scan.isPending ? "Scanning…" : "Find pairs"}</button>
      </div>
      {scan.error && <div className="pnl-neg">{String((scan.error as Error).message)}</div>}
      {r && (
        <>
          <div className="meta" style={{ margin: "6px 0" }} data-testid="pair-summary">
            {r.tested} pairs tested in {r.elapsed_s}s: {r.cointegrated} cointegrated on the first two thirds (≈{r.expected_by_luck} expected by luck alone),{" "}
            <b>{r.confirmed} still hold on the last third</b>, which the fit never saw. Trust only those; a pair you load starts at {r.tested} tries.
            {r.notes.length > 0 && <> Skipped: {r.notes.map((n) => `${short(n.symbol)} (${n.note})`).join("; ")}.</>}
          </div>
          <div className="algo-scroll">
            <table>
              <thead><tr><th>Trade</th><th>vs</th><th title="hedge ratio: A's % move per 1% of B">β</th><th title="correlation of one-bar returns">corr</th>
                <th title="Engle-Granger t on the first two thirds; below -3.34 = cointegrated at 5%">ADF fit</th>
                <th title="the same fit's spread on the last third; below -2.86 = it still snaps back">ADF held-out</th>
                <th title="bars for a gap in the spread to halve">half-life</th><th title="how far A is from its fitted value now, in deviations">z now</th><th>window</th><th></th></tr></thead>
              <tbody>{r.pairs.map((p) => (
                <tr key={`${p.a}|${p.b}`}>
                  <td>{short(p.a)}</td><td>{short(p.b)}</td><td className="mono">{num(p.beta)}</td><td className="mono">{num(p.corr)}</td>
                  <td className={`mono ${p.cointegrated ? "pnl-pos" : ""}`}>{num(p.adf_fit)}</td>
                  <td className={`mono ${p.holds_out ? "pnl-pos" : p.cointegrated ? "pnl-neg" : ""}`}>{num(p.adf_held_out)}</td>
                  <td className="mono">{num(p.half_life, 1)}</td><td className="mono">{num(p.z_now)}</td><td className="mono">{p.window}</td>
                  <td>{p.cointegrated && p.holds_out
                    ? <button type="button" className="btn-sm" onClick={() => onUse(p.spec, `Pair ${short(p.a)} vs ${short(p.b)}`, r.tested)}>→ builder</button>
                    : <span className="meta">{p.cointegrated ? "didn't hold" : "—"}</span>}</td>
                </tr>))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
