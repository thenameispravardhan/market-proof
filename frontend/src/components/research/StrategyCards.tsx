/**
 * B1 research loop: the strategy card one research check produces, and the
 * Research tab listing every card — what was tried, how many times, and
 * whether it survived the out-of-sample bar (app/research/loop.py).
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { PairFinder } from "./PairFinder";
import { SurfaceCheck, TickReplay } from "./SurfacePanels";
import { UniversePanel } from "./UniversePanel";
import { NightlyPanel } from "./NightlyPanel";

export type Check = { key: string; label: string; value: number | null; pass: boolean | null; note: string | null };
type Sum = { trades: number; win_rate: number; net_pnl: number; profit_factor: number | null; sharpe: number | null; max_drawdown?: number; max_drawdown_pct?: number };
type Fold = { is_from: number; oos_from: number; oos_to: number; params: Record<string, number> | null; oos: Sum | null };
export type CardSummary = {
  id: number; strategy_id: number | null; version: number | null; name: string; trials: number; verdict: "pass" | "kill";
  checks: Check[]; hypothesis: string | null; note: string | null; created_at: string | null; kind?: "check" | "nightly";
  setup: { start: string; end: string; grid: { path: string; values: number[] }[]; metric: string; min_trades: number; oos_pct: number; walk_forward: { windows: number; is_mult: number; anchored: boolean } };
  headline: { dsr: number | null; pbo: number | null; wf_net: number | null; held_out_net: number | null };
};
export type ResearchCard = CardSummary & {
  rules: string[];
  result: {
    combos: number; trials: number; fixed: boolean;
    split: { params: Record<string, number>; oos_from: number; is: Sum; oos: Sum; ranked: number } | null;
    walk_forward: { windows: number; anchored: boolean; folds: Fold[]; oos: Sum; wfe_pct: number | null; param_sets: number };
    full: (Sum & { max_drawdown_pct: number }) | null;
  };
  elapsed_s?: number;
};

const inr = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `₹${Math.round(v).toLocaleString("en-IN")}`);
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(0)}%`);
const num = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toFixed(2));
const pnlCls = (v: number | null | undefined) => (!v ? "" : v > 0 ? "pnl-pos" : "pnl-neg");
const day = (e: number) => new Date(e * 1000).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "2-digit" });

export function Verdict({ v }: { v: "pass" | "kill" }) {
  return <span className={`badge ${v === "pass" ? "positive" : "negative"}`} data-testid="card-verdict">{v === "pass" ? "PASS" : "KILLED"}</span>;
}

function checkValue(c: Check) {
  return c.key === "dsr" || c.key === "pbo" ? pct(c.value) : inr(c.value);
}

/** One page per idea: rules, tries, the bar, out-of-sample P&L. */
export function StrategyCard({ card }: { card: ResearchCard }) {
  const qc = useQueryClient();
  const r = card.result;
  const [note, setNote] = useState(card.note ?? "");
  const save = useMutation({
    mutationFn: () => fetch(`/api/algo/research/cards/${card.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note }),
    }).then((x) => { if (!x.ok) throw new Error(`HTTP ${x.status}`); return x.json(); }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["algo", "research"] }),
  });
  const exportMd = async () => {
    const res = await fetch(`/api/algo/research/cards/${card.id}/markdown`);
    const blob = new Blob([await res.text()], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `card-${card.id}-${card.name.replace(/[^\w-]+/g, "_")}.md`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  const w = r.walk_forward;
  return (
    <div data-testid="strategy-card" style={{ display: "grid", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Verdict v={card.verdict} />
        <b>{card.name}</b>
        <span className="meta">card #{card.id} · {card.strategy_id ? `strategy #${card.strategy_id}${card.version ? ` v${card.version}` : ""}` : "not saved"} · {card.setup.start} → {card.setup.end} · {card.trials} tries on this idea{card.elapsed_s !== undefined ? ` · ${card.elapsed_s}s` : ""}</span>
        <button type="button" className="btn-sm" style={{ marginLeft: "auto" }} onClick={exportMd}>⬇ card (.md)</button>
      </div>
      {card.hypothesis && <div><span className="meta">Idea:</span> {card.hypothesis}</div>}
      <ul className="meta" style={{ margin: 0, paddingLeft: 18 }}>
        {card.rules.map((x, i) => <li key={i}>{x}</li>)}
        {card.setup.grid.length > 0 && <li>Grid: {card.setup.grid.map((g) => `${g.path} ∈ [${g.values.join(", ")}]`).join("; ")}</li>}
      </ul>
      <table>
        <thead><tr><th>The bar</th><th>Value</th><th>Result</th></tr></thead>
        <tbody>{card.checks.map((c) => (
          <tr key={c.key} data-testid={`check-${c.key}`}>
            <td>{c.label}</td>
            <td className="mono">{checkValue(c)}</td>
            <td className={c.pass ? "pnl-pos" : c.pass === null ? "meta" : "pnl-neg"}>{c.pass ? "pass" : c.pass === null ? "n/a" : "FAIL"}{c.note ? ` — ${c.note}` : ""}</td>
          </tr>))}
        </tbody>
      </table>
      <table>
        <thead><tr><th>Out of sample</th><th>Trades</th><th>Win %</th><th>PF</th><th>Sharpe</th><th>Net</th><th></th></tr></thead>
        <tbody>
          {r.split && (
            <tr>
              <td>Held-out last {card.setup.oos_pct}% (from {day(r.split.oos_from)})</td>
              <td className="mono">{r.split.oos.trades}</td><td className="mono">{num(r.split.oos.win_rate)}</td><td className="mono">{num(r.split.oos.profit_factor)}</td>
              <td className="mono">{num(r.split.oos.sharpe)}</td><td className={`mono ${pnlCls(r.split.oos.net_pnl)}`}>{inr(r.split.oos.net_pnl)}</td>
              <td className="meta">{Object.keys(r.split.params).length ? `chosen ${Object.entries(r.split.params).map(([k, v]) => `${k.split(".").slice(-2).join(".")}=${v}`).join(", ")} · ` : ""}in-sample {inr(r.split.is.net_pnl)}</td>
            </tr>
          )}
          <tr>
            <td>Walk-forward, {w.windows} {w.anchored ? "anchored" : "rolling"} windows</td>
            <td className="mono">{w.oos.trades}</td><td className="mono">{num(w.oos.win_rate)}</td><td className="mono">{num(w.oos.profit_factor)}</td>
            <td className="mono">{num(w.oos.sharpe)}</td><td className={`mono ${pnlCls(w.oos.net_pnl)}`}>{inr(w.oos.net_pnl)}</td>
            <td className="meta">efficiency {w.wfe_pct === null ? "—" : `${w.wfe_pct}%`}{r.fixed ? "" : ` · ${w.param_sets} parameter set${w.param_sets === 1 ? "" : "s"} across windows`}</td>
          </tr>
          {r.full && (
            <tr>
              <td>Whole range{r.fixed ? "" : ", chosen combo"}</td>
              <td className="mono">{r.full.trades}</td><td className="mono">{num(r.full.win_rate)}</td><td className="mono">{num(r.full.profit_factor)}</td>
              <td className="mono">{num(r.full.sharpe)}</td><td className={`mono ${pnlCls(r.full.net_pnl)}`}>{inr(r.full.net_pnl)}</td>
              <td className="meta">max DD {num(r.full.max_drawdown_pct)}%</td>
            </tr>
          )}
        </tbody>
      </table>
      <div className="meta">
        {w.folds.map((f, i) => (
          <span key={i} style={{ marginRight: 12 }}>W{i + 1} {day(f.oos_from)}: <span className={pnlCls(f.oos?.net_pnl)}>{f.params ? inr(f.oos?.net_pnl) : "not traded"}</span></span>
        ))}
      </div>
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <input value={note} placeholder="note — e.g. what to try next, why it was killed" onChange={(e) => setNote(e.target.value)} style={{ flex: 1 }} />
        <button type="button" className="btn-sm" disabled={save.isPending || note === (card.note ?? "")} onClick={() => save.mutate()}>save note</button>
      </div>
      {card.verdict === "kill"
        ? <div className="meta">Killed: below the bar. Don't switch it on — change the idea (not just the numbers) and check again; every re-check adds to the tries the Deflated Sharpe deflates by.</div>
        : <div className="meta">Passed: paper-trade it next. Only real money after its paper run matches this card.</div>}
    </div>
  );
}

/** The pair finder, then every card, newest first; click one to open it. */
export function ResearchTab({ timeframes, onUse, spec, range }: { timeframes: number[]; onUse: (spec: Record<string, unknown>, name: string, tries: number) => void;
  spec: unknown; range: { start: string; end: string } }) {
  const [verdict, setVerdict] = useState<"" | "pass" | "kill">("");
  const [kind, setKind] = useState<"" | "check" | "nightly">("");
  const [open, setOpen] = useState<number | null>(null);
  const list = useQuery({
    queryKey: ["algo", "research", "cards", verdict, kind],
    queryFn: () => api.get<{ cards: CardSummary[]; bar: { dsr: number; pbo: number } }>(
      `/api/algo/research/cards?${new URLSearchParams({ ...(verdict ? { verdict } : {}), ...(kind ? { kind } : {}) })}`),
  });
  const card = useQuery({
    queryKey: ["algo", "research", "card", open], enabled: open !== null,
    queryFn: () => api.get<ResearchCard>(`/api/algo/research/cards/${open}`),
  });
  const cards = list.data?.cards ?? [];
  const bar = list.data?.bar;
  return (
    <>
    <PairFinder timeframes={timeframes} onUse={onUse} />
    <SurfaceCheck range={range} />
    <TickReplay spec={spec} range={range} />
    <UniversePanel />
    <NightlyPanel onOpenCard={setOpen} />
    <div className="widget widget-wide" data-testid="research-tab">
      <h3>Strategy cards <span className="meta">one per research check — run one from Optimise → “research check”</span></h3>
      <div className="meta" style={{ marginBottom: 8 }}>
        The bar: Deflated Sharpe ≥ {bar ? pct(bar.dsr) : "95%"} after every try on the idea, the walk-forward's unseen windows and the held-out period both net positive, PBO ≤ {bar ? pct(bar.pbo) : "50%"} when a grid was swept.
        Anything below it is killed.{" "}
        <label>show <select value={verdict} onChange={(e) => setVerdict(e.target.value as "" | "pass" | "kill")}>
          <option value="">all</option><option value="pass">passed</option><option value="kill">killed</option>
        </select></label>{" "}
        <label><select value={kind} onChange={(e) => setKind(e.target.value as "" | "check" | "nightly")} data-testid="card-kind">
          <option value="">checks and nightly</option><option value="check">research checks</option><option value="nightly">nightly re-validations</option>
        </select></label>
      </div>
      {list.error && <div className="pnl-neg">{String(list.error)}</div>}
      {!cards.length && !list.isLoading && <div className="meta">No cards yet.</div>}
      {cards.length > 0 && (
        <div className="algo-scroll">
          <table>
            <thead><tr><th>#</th><th>When</th><th>Idea</th><th>Verdict</th><th>Tries</th><th>DSR</th><th>PBO</th><th>Walk-forward net</th><th>Held-out net</th></tr></thead>
            <tbody>{cards.map((c) => (
              <tr key={c.id} onClick={() => setOpen(open === c.id ? null : c.id)} style={{ cursor: "pointer" }} className={open === c.id ? "active" : ""}>
                <td className="mono">{c.id}</td>
                <td className="mono">{c.created_at ? new Date(c.created_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "short", timeStyle: "short" }) : "—"}</td>
                <td>{c.name}{c.strategy_id === null ? <span className="meta"> (draft)</span> : null}{c.kind === "nightly" ? <span className="badge neutral" style={{ marginLeft: 4 }}>nightly</span> : null}</td>
                <td><Verdict v={c.verdict} /></td>
                <td className="mono">{c.trials}</td>
                <td className="mono">{pct(c.headline.dsr)}</td>
                <td className="mono">{pct(c.headline.pbo)}</td>
                <td className={`mono ${pnlCls(c.headline.wf_net)}`}>{inr(c.headline.wf_net)}</td>
                <td className={`mono ${pnlCls(c.headline.held_out_net)}`}>{inr(c.headline.held_out_net)}</td>
              </tr>))}
            </tbody>
          </table>
        </div>
      )}
      {open !== null && card.data && <div style={{ marginTop: 12 }}><StrategyCard key={card.data.id} card={card.data} /></div>}
    </div>
    </>
  );
}
