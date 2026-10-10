// Research page — the evidence behind the strategy, on one screen.
//
// Everything the bot is judged on lives here, ordered the way an examiner
// would ask:
//
//   1. Decision funnel  — what share of signals is blocked, and by which rule.
//   2. Calibration      — does the model's confidence mean anything? Per model,
//                         with AUC (ranking) and ECE (honesty of the number).
//   3. Cost-aware replay — what the signals would have earned after charges,
//                         split by block reason and confidence bucket.
//   4. Shadow model     — live model vs the SLM on the same filings.
//   5. Evaluation window — a pre-registered, frozen-config paper window.
//   6. Audit trail      — verify the hash chain; look up one decision's "why".
//
// All of it is read-only: nothing on this page can place, block or size a trade.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

const pct = (v: number | null | undefined, dp = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(dp)}%`;
const num = (v: number | null | undefined, dp = 3) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : v.toFixed(dp);
const ci = (c: (number | null)[] | undefined, dp = 3) =>
  c && c[0] !== null && c[1] !== null ? `[${num(c[0], dp)}, ${num(c[1], dp)}]` : "—";

// One single-series horizontal bar (0..1 of the track). Text stays in ink
// tokens; the bar carries the magnitude, the title attribute the exact value.
function Bar({ value, label }: { value: number | null | undefined; label: string }) {
  const w = Math.max(0, Math.min(1, value ?? 0)) * 100;
  return (
    <div className="res-track" title={label} aria-label={label} role="img" style={{ minWidth: 80 }}>
      <div className="res-fill" style={{ width: `${w}%`, color: "var(--accent)" }} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 1. Funnel
// ---------------------------------------------------------------------------

interface Funnel {
  signals: number;
  blocked: number;
  block_rate: number | null;
  by_block_reason: { reason: string; n: number; share_of_blocked: number | null }[];
}

export function FunnelPanel({ days }: { days: number }) {
  const { data, isLoading } = useQuery<Funnel>({
    queryKey: ["research", "funnel", days],
    queryFn: () => api.get(`/api/research/funnel?days=${days}`),
  });
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }}>
      <h3>Decision funnel</h3>
      {isLoading || !data ? <div className="meta">Loading…</div> : (
        <>
          <div className="meta" style={{ marginBottom: 8 }}>
            {data.signals.toLocaleString("en-IN")} signals in {days} days · {data.blocked.toLocaleString("en-IN")} blocked ({pct(data.block_rate)})
          </div>
          <table>
            <thead><tr><th>Block reason</th><th>Signals</th><th>Share of blocked</th><th style={{ width: "40%" }} /></tr></thead>
            <tbody>
              {data.by_block_reason.map((r) => (
                <tr key={r.reason}>
                  <td className="mono">{r.reason}</td>
                  <td className="mono">{r.n.toLocaleString("en-IN")}</td>
                  <td className="mono">{pct(r.share_of_blocked)}</td>
                  <td><Bar value={r.share_of_blocked} label={`${r.reason}: ${pct(r.share_of_blocked)} of blocked`} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 2. Calibration
// ---------------------------------------------------------------------------

interface CalBlock {
  ece: number | null;
  n: number;
  base_rate: number | null;
  auc: number | null;
  bins: { lo: number; hi: number; n: number; mean_confidence: number | null; observed_rate: number | null }[];
}
interface Calibration {
  overall: CalBlock;
  by_model: Record<string, CalBlock>;
  by_month: { month: string; n: number; ece: number | null; auc: number | null; base_rate: number | null }[];
  mover_pct: number;
}

export function aucVerdict(auc: number | null | undefined): string {
  if (auc === null || auc === undefined) return "not enough data";
  if (auc < 0.48) return "INVERTED: higher confidence, fewer movers";
  if (auc <= 0.52) return "no ranking information";
  return "ranks movers above non-movers";
}

function CalibrationPanel({ days }: { days: number }) {
  const { data, isLoading } = useQuery<Calibration>({
    queryKey: ["research", "calibration", days],
    queryFn: () => api.get(`/api/research/calibration?days=${days}`),
  });
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }}>
      <h3>Confidence calibration</h3>
      {isLoading || !data ? <div className="meta">Loading…</div> : (
        <>
          <div className="meta" style={{ marginBottom: 8 }}>
            Outcome: the filing moved at least {data.mover_pct}% in 30 minutes. {data.overall.n.toLocaleString("en-IN")} outcomes,
            base rate {pct(data.overall.base_rate)}, AUC {num(data.overall.auc)} ({aucVerdict(data.overall.auc)}), ECE {num(data.overall.ece)}.
          </div>
          <table>
            <thead><tr><th>Stated confidence</th><th>n</th><th>Mean stated</th><th>Actually moved</th><th style={{ width: "35%" }} /></tr></thead>
            <tbody>
              {data.overall.bins.filter((b) => b.n > 0).map((b) => (
                <tr key={b.lo}>
                  <td className="mono">{b.lo.toFixed(1)}–{b.hi.toFixed(1)}</td>
                  <td className="mono">{b.n}</td>
                  <td className="mono">{pct(b.mean_confidence)}</td>
                  <td className="mono">{pct(b.observed_rate)}</td>
                  <td><Bar value={b.observed_rate} label={`stated ${pct(b.mean_confidence)}, moved ${pct(b.observed_rate)}`} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          <h4 style={{ margin: "12px 0 4px" }}>By model</h4>
          <table>
            <thead><tr><th>Model</th><th>n</th><th>AUC</th><th>ECE</th><th>Verdict</th></tr></thead>
            <tbody>
              {Object.entries(data.by_model).map(([m, b]) => (
                <tr key={m}><td className="mono">{m}</td><td className="mono">{b.n}</td><td className="mono">{num(b.auc)}</td>
                  <td className="mono">{num(b.ece)}</td><td>{aucVerdict(b.auc)}</td></tr>
              ))}
            </tbody>
          </table>
          {data.by_month.length > 1 && (
            <>
              <h4 style={{ margin: "12px 0 4px" }}>Drift by month</h4>
              <table>
                <thead><tr><th>Month</th><th>n</th><th>Base rate</th><th>AUC</th><th>ECE</th></tr></thead>
                <tbody>{data.by_month.map((m) => (
                  <tr key={m.month}><td className="mono">{m.month}</td><td className="mono">{m.n}</td>
                    <td className="mono">{pct(m.base_rate)}</td><td className="mono">{num(m.auc)}</td><td className="mono">{num(m.ece)}</td></tr>
                ))}</tbody>
              </table>
            </>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 3. Replay
// ---------------------------------------------------------------------------

interface RBlock {
  n: number; win_rate: number | null; expectancy_r: number | null; expectancy_r_ci95: (number | null)[];
  ci_excludes_zero: boolean; net_pnl: number; profit_factor: number | null; max_drawdown: number;
}
interface Replay {
  events_loaded: number;
  skipped: Record<string, number>;
  all_directional: RBlock & { daily_sharpe: number | null };
  taken: RBlock & { daily_sharpe: number | null };
  blocked: RBlock;
  by_block_reason: Record<string, RBlock>;
  confidence_buckets: { bucket: string; n: number; mean_pre_move_pct: number | null; mean_post_move_pct: number | null; expectancy_r: number | null }[];
  breakeven_pct_at_notional: number;
}

function RBlockRow({ label, b }: { label: string; b: RBlock }) {
  return (
    <tr>
      <td>{label}</td>
      <td className="mono">{b.n}</td>
      <td className="mono">{pct(b.win_rate)}</td>
      <td className="mono">{num(b.expectancy_r)}</td>
      <td className="mono">{ci(b.expectancy_r_ci95)}{b.ci_excludes_zero ? " *" : ""}</td>
      <td className="mono">₹{b.net_pnl.toLocaleString("en-IN")}</td>
    </tr>
  );
}

function ReplayPanel({ days }: { days: number }) {
  const [delay, setDelay] = useState(20);
  const [slip, setSlip] = useState(5);
  const run = useMutation({
    mutationFn: () => api.get<Replay>(`/api/research/replay?days=${days}&entry_delay_s=${delay}&slippage_bps=${slip}`),
  });
  const d = run.data;
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }}>
      <h3>Cost-aware replay</h3>
      <div className="meta" style={{ marginBottom: 8 }}>
        Replays recorded signals on 1-minute candles with the bot's own stop, target, hold window and Indian intraday charges.
        Blocked and HOLD signals are replayed too, so each gate shows what it saved or cost.
      </div>
      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 8, flexWrap: "wrap" }}>
        <label className="meta">entry delay (s) <input type="number" value={delay} min={0} max={600} style={{ width: 64 }} onChange={(e) => setDelay(Number(e.target.value))} /></label>
        <label className="meta">slippage (bps) <input type="number" value={slip} min={0} max={500} style={{ width: 64 }} onChange={(e) => setSlip(Number(e.target.value))} /></label>
        <button type="button" className="primary" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Replaying…" : "Run replay"}</button>
      </div>
      {run.isError && <div className="pnl-neg">{(run.error as Error).message}</div>}
      {d && (
        <>
          <div className="meta" style={{ marginBottom: 6 }}>
            {d.events_loaded} events loaded; skipped {Object.entries(d.skipped).map(([k, v]) => `${v} ${k}`).join(", ") || "none"}.
            Break-even per round trip ≈ {d.breakeven_pct_at_notional.toFixed(3)}% before slippage. * = 95% CI excludes zero.
          </div>
          <table>
            <thead><tr><th>Set</th><th>Trades</th><th>Win rate</th><th>E[R]</th><th>95% CI</th><th>Net</th></tr></thead>
            <tbody>
              <RBlockRow label="Every directional signal" b={d.all_directional} />
              <RBlockRow label="Taken by the bot" b={d.taken} />
              <RBlockRow label="Blocked" b={d.blocked} />
              {Object.entries(d.by_block_reason).map(([k, b]) => <RBlockRow key={k} label={`  blocked: ${k}`} b={b} />)}
            </tbody>
          </table>
          <h4 style={{ margin: "12px 0 4px" }}>Was the move already gone? (by confidence)</h4>
          <table>
            <thead><tr><th>Confidence</th><th>n</th><th>Move before entry</th><th>Move after entry</th><th>E[R]</th></tr></thead>
            <tbody>{d.confidence_buckets.map((b) => (
              <tr key={b.bucket}><td className="mono">{b.bucket}</td><td className="mono">{b.n}</td>
                <td className="mono">{num(b.mean_pre_move_pct, 2)}%</td><td className="mono">{num(b.mean_post_move_pct, 2)}%</td>
                <td className="mono">{num(b.expectancy_r)}</td></tr>
            ))}</tbody>
          </table>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 4. Shadow
// ---------------------------------------------------------------------------

interface Shadow {
  shadow_calls: Record<string, number>;
  shadow_latency_ms: { p50: number | null; p90: number | null };
  paired: number;
  live_model: string | null;
  recommendation_agreement: number | null;
  auc_live_vs_shadow: { auc_a: number | null; auc_b: number | null; diff: number | null; ci95: (number | null)[]; ci_excludes_zero?: boolean } | null;
}

function ShadowPanel({ days }: { days: number }) {
  const { data } = useQuery<Shadow>({
    queryKey: ["research", "shadow", days],
    queryFn: () => api.get(`/api/research/shadow?days=${days}`),
  });
  if (!data) return null;
  const c = data.auc_live_vs_shadow;
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }}>
      <h3>Shadow model (SLM) vs live model</h3>
      {data.paired === 0 ? (
        <div className="meta">No paired filings yet. Turn on Settings → AI analysis → "Shadow-score every filing with the SLM" with an SLM endpoint configured.
          Calls so far: {Object.entries(data.shadow_calls).map(([k, v]) => `${v} ${k}`).join(", ") || "none"}.</div>
      ) : (
        <table>
          <tbody>
            <tr><td>Paired filings with a 30-minute outcome</td><td className="mono">{data.paired}</td></tr>
            <tr><td>AUC — live ({data.live_model ?? "?"})</td><td className="mono">{num(c?.auc_a)}</td></tr>
            <tr><td>AUC — shadow SLM</td><td className="mono">{num(c?.auc_b)}</td></tr>
            <tr><td>Difference (paired bootstrap 95% CI)</td><td className="mono">{num(c?.diff)} {ci(c?.ci95)}</td></tr>
            <tr><td>Verdict</td><td>{c?.ci_excludes_zero && (c.diff ?? 0) > 0 ? "SLM is better (CI clears zero)" : "No proven difference — keep the live model"}</td></tr>
            <tr><td>Recommendation agreement</td><td className="mono">{pct(data.recommendation_agreement)}</td></tr>
            <tr><td>Shadow latency p50 / p90</td><td className="mono">{num(data.shadow_latency_ms.p50, 0)} / {num(data.shadow_latency_ms.p90, 0)} ms</td></tr>
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 5. Evaluation window
// ---------------------------------------------------------------------------

interface Window {
  id: number; name: string; hypothesis: string; status: string; started_at: string; ends_at: string;
  days_elapsed: number; days_planned: number; frozen: boolean; evidence_verdict: string;
  violation: { at: string; changes: string[] } | null;
  signals: number; signals_blocked: number;
  closed_trades: { n: number; net_pnl: number; expectancy_r?: number | null; expectancy_r_ci95?: (number | null)[] };
}

function WindowPanel() {
  const qc = useQueryClient();
  const { data } = useQuery<{ windows: Window[] }>({
    queryKey: ["research", "windows"],
    queryFn: () => api.get("/api/research/windows"),
  });
  const [name, setName] = useState("");
  const [hyp, setHyp] = useState("");
  const [weeks, setWeeks] = useState(6);
  const start = useMutation({
    mutationFn: () => api.post("/api/research/windows", { name, hypothesis: hyp, weeks }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["research", "windows"] }),
  });
  const close = useMutation({
    mutationFn: (id: number) => api.post(`/api/research/windows/${id}/close`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["research", "windows"] }),
  });
  const active = data?.windows.find((w) => w.status === "active");
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }}>
      <h3>Pre-registered evaluation window</h3>
      <div className="meta" style={{ marginBottom: 8 }}>
        Declare a hypothesis and a length before the window starts. Every decision-relevant setting, rule and prompt is hashed;
        a change while the window runs marks it invalid as out-of-sample evidence.
      </div>
      {active ? (
        <div>
          <div><strong>{active.name}</strong> — day {active.days_elapsed} of {active.days_planned}</div>
          <div className="meta">Hypothesis: {active.hypothesis}</div>
          <div className={active.frozen ? "pnl-pos" : "pnl-neg"} style={{ margin: "6px 0" }}>{active.evidence_verdict}</div>
          {active.violation && <ul className="meta">{active.violation.changes.slice(0, 8).map((c) => <li key={c}>{c}</li>)}</ul>}
          <div className="meta">{active.signals} signals ({active.signals_blocked} blocked) · {active.closed_trades.n} closed trades · net ₹{active.closed_trades.net_pnl.toLocaleString("en-IN")}
            {active.closed_trades.expectancy_r !== undefined && ` · E[R] ${num(active.closed_trades.expectancy_r)} ${ci(active.closed_trades.expectancy_r_ci95)}`}</div>
          <button type="button" className="btn-sm" style={{ marginTop: 6 }} disabled={close.isPending} onClick={() => close.mutate(active.id)}>Close window</button>
        </div>
      ) : (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <input placeholder="name" value={name} onChange={(e) => setName(e.target.value)} style={{ width: 140 }} />
          <input placeholder="hypothesis, e.g. taken trades have E[R] > 0 after costs" value={hyp} onChange={(e) => setHyp(e.target.value)} style={{ flex: 1, minWidth: 220 }} />
          <label className="meta">weeks <input type="number" min={1} max={26} value={weeks} onChange={(e) => setWeeks(Number(e.target.value))} style={{ width: 52 }} /></label>
          <button type="button" className="primary" disabled={!hyp.trim() || start.isPending} onClick={() => start.mutate()}>Start window</button>
          {start.isError && <span className="pnl-neg">{(start.error as Error).message}</span>}
        </div>
      )}
      {data && data.windows.filter((w) => w.status !== "active").length > 0 && (
        <table style={{ marginTop: 10 }}>
          <thead><tr><th>Past window</th><th>Days</th><th>Closed trades</th><th>Net</th><th>Evidence</th></tr></thead>
          <tbody>{data.windows.filter((w) => w.status !== "active").map((w) => (
            <tr key={w.id}><td>{w.name}</td><td className="mono">{w.days_elapsed}</td><td className="mono">{w.closed_trades.n}</td>
              <td className="mono">₹{w.closed_trades.net_pnl.toLocaleString("en-IN")}</td><td>{w.evidence_verdict}</td></tr>
          ))}</tbody>
        </table>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 6. Audit + why card
// ---------------------------------------------------------------------------

interface Verify { ok: boolean; checked: number; first_bad_id: number | null; reason: string | null; head_hash: string }
interface Why {
  signal: { id: number; symbol: string; action: string; status: string; confidence: number | null; rule_rationale: string };
  block_reason: string | null;
  filing: { headline: string; event_type: string; exchange: string; pdf_url: string | null; filed_at: string | null } | null;
  analysis: { model: string; recommendation: string | null; confidence: number | null; summary: string | null; reasoning: string | null; key_numbers: Record<string, unknown> | null; saw_filing_text: boolean } | null;
  risk_checks_blocked: { code: string; message: string }[];
  outcome: { move_5m_pct: number | null; move_30m_pct: number | null } | null;
}

function AuditPanel() {
  const verify = useMutation({ mutationFn: () => api.get<Verify>("/api/audit-log/verify") });
  const [sid, setSid] = useState("");
  const why = useMutation({ mutationFn: (id: string) => api.get<Why>(`/api/research/why/${encodeURIComponent(id)}`) });
  const w = why.data;
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }}>
      <h3>Audit trail and decision lookup</h3>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8, flexWrap: "wrap" }}>
        <button type="button" className="btn-sm" disabled={verify.isPending} onClick={() => verify.mutate()}>Verify audit hash chain</button>
        {verify.data && (verify.data.ok
          ? <span className="pnl-pos">Intact — {verify.data.checked.toLocaleString("en-IN")} entries, head {verify.data.head_hash.slice(0, 16)}…</span>
          : <span className="pnl-neg">BROKEN at entry {verify.data.first_bad_id}: {verify.data.reason}</span>)}
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <input placeholder="signal id" value={sid} onChange={(e) => setSid(e.target.value)} style={{ width: 100 }} />
        <button type="button" className="btn-sm" disabled={!sid.trim() || why.isPending} onClick={() => why.mutate(sid.trim())}>Why?</button>
        {why.isError && <span className="pnl-neg">{(why.error as Error).message}</span>}
      </div>
      {w && (
        <div style={{ marginTop: 10 }} data-testid="why-card">
          <div><strong>{w.signal.symbol}</strong> {w.signal.action} — {w.signal.status}{w.block_reason ? ` (${w.block_reason})` : ""}</div>
          {w.filing && <div className="meta">{w.filing.event_type} · {w.filing.exchange} · {w.filing.headline}</div>}
          {w.analysis && (
            <div style={{ marginTop: 4 }}>
              <div className="meta">{w.analysis.model} said {w.analysis.recommendation ?? "?"} at confidence {pct(w.analysis.confidence, 0)}
                {w.analysis.saw_filing_text ? " after reading the filing text" : " from the headline only"}.</div>
              {w.analysis.summary && <div>{w.analysis.summary}</div>}
              {w.analysis.reasoning && <div className="meta">{w.analysis.reasoning}</div>}
              {w.analysis.key_numbers && Object.keys(w.analysis.key_numbers).length > 0 && (
                <div className="meta mono">{Object.entries(w.analysis.key_numbers).map(([k, v]) => `${k}=${String(v)}`).join(" · ")}</div>
              )}
            </div>
          )}
          <div className="meta" style={{ marginTop: 4 }}>Rule: {w.signal.rule_rationale || "—"}</div>
          {w.risk_checks_blocked.length > 0 && (
            <ul className="meta">{w.risk_checks_blocked.map((r, i) => <li key={i}><span className="mono">{r.code}</span>: {r.message}</li>)}</ul>
          )}
          {w.outcome && <div className="meta">Afterwards: {num(w.outcome.move_5m_pct, 2)}% at 5 min, {num(w.outcome.move_30m_pct, 2)}% at 30 min.</div>}
        </div>
      )}
    </div>
  );
}

const DAY_OPTIONS = [30, 90, 180, 365];

export default function Research() {
  const [days, setDays] = useState(90);
  return (
    <div className="research-page">
      <div className="dashboard-head">
        <h1 className="page-title">Research</h1>
        <label className="meta">window{" "}
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {DAY_OPTIONS.map((d) => <option key={d} value={d}>{d} days</option>)}
          </select>
        </label>
      </div>
      <FunnelPanel days={days} />
      <CalibrationPanel days={days} />
      <ReplayPanel days={days} />
      <ShadowPanel days={days} />
      <WindowPanel />
      <AuditPanel />
    </div>
  );
}
