// ChartInfoDialogs — the chart's informational / management dialogs:
//   AboutIndicatorDialog  legend More → About: what an indicator computes
//   ManagePanesDialog     reorder, merge, maximise, collapse and delete panes
//   InsightsDialog        a technical read of the symbol from its daily bars
//                         (trend, momentum, volatility, volume, levels, patterns,
//                         filed announcements) — computed here, no extra feed

import { useEffect, useState } from "react";
import { atr, ema, macd, rsi, sma, technicalRating, type OhlcvCandle } from "../../lib/indicators";
import type { IndicatorDef } from "./indicatorCatalog";
import { candlePatterns } from "./indicatorMore";
import { Modal } from "./chartUi";

export function AboutIndicatorDialog({ def, onClose }: { def: IndicatorDef; onClose: () => void }) {
  return (
    <Modal title={`About · ${def.name}`} onClose={onClose} width={460} testid="ind-about">
      <p className="about-desc">{def.desc}</p>
      <div className="about-grid">
        <span className="hint">Category</span>
        <span>{def.category}{def.desk ? " · Fyers indicators" : ""}</span>
        <span className="hint">Placement</span>
        <span>{def.overlay ? "On the price pane" : "In its own pane"}{def.intradayOnly ? " · intraday charts only" : ""}</span>
        {def.inputs.length > 0 && (
          <>
            <span className="hint">Inputs</span>
            <span>{def.inputs.map((i) => `${i.label} = ${String(i.def)}`).join(" · ")}</span>
          </>
        )}
        {def.plots.length > 0 && (
          <>
            <span className="hint">Outputs</span>
            <span className="about-plots">
              {def.plots.filter((p) => p.kind !== "marks").map((p) => (
                <span key={p.key}><i style={{ background: p.color }} />{p.label}</span>
              ))}
              {def.plots.some((p) => p.kind === "marks") && <span><i style={{ background: "#787B86" }} />markers on bars</span>}
            </span>
          </>
        )}
        {def.levels && (
          <>
            <span className="hint">Levels</span>
            <span>{def.levels.join(" / ")}</span>
          </>
        )}
      </div>
    </Modal>
  );
}

export interface PaneInfo {
  i: number;
  names: string[];
}

export function ManagePanesDialog({
  panes,
  maxPane,
  collapsed,
  onMove,
  onMerge,
  onDelete,
  onMax,
  onCollapse,
  onClose,
}: {
  panes: PaneInfo[];
  maxPane: number | null;
  collapsed: number[];
  onMove: (i: number, by: -1 | 1) => void;
  onMerge: (from: number, to: number) => void;
  onDelete: (i: number) => void;
  onMax: (i: number) => void;
  onCollapse: (i: number) => void;
  onClose: () => void;
}) {
  const last = Math.max(0, ...panes.map((p) => p.i));
  return (
    <Modal title="Manage panes" onClose={onClose} width={560} testid="manage-panes">
      {panes.length <= 1 && <div className="hint">Only the price pane — add an oscillator (RSI, MACD …) to get more panes.</div>}
      <table className="panes-table">
        <thead>
          <tr><th>#</th><th>Contents</th><th>Order</th><th>Merge into</th><th /></tr>
        </thead>
        <tbody>
          {panes.map((p) => (
            <tr key={p.i} data-testid={`pane-row-${p.i}`}>
              <td>{p.i === 0 ? "Price" : p.i}</td>
              <td>{p.i === 0 ? ["Main series", ...p.names].join(", ") : p.names.join(", ")}</td>
              <td>
                <button type="button" className="cbtn" disabled={p.i <= 1} onClick={() => onMove(p.i, -1)} title="Move up">▲</button>
                <button type="button" className="cbtn" disabled={p.i === 0 || p.i >= last} onClick={() => onMove(p.i, 1)} title="Move down">▼</button>
              </td>
              <td>
                {p.i > 0 && (
                  <select className="cform-sel" value="" onChange={(e) => e.target.value !== "" && onMerge(p.i, Number(e.target.value))} aria-label={`Merge pane ${p.i}`}>
                    <option value="">—</option>
                    {panes.filter((q) => q.i !== p.i).map((q) => <option key={q.i} value={q.i}>{q.i === 0 ? "Price pane" : `Pane ${q.i}`}</option>)}
                  </select>
                )}
              </td>
              <td className="panes-acts">
                <button type="button" className={`cbtn${maxPane === p.i ? " primary" : ""}`} onClick={() => onMax(p.i)} title={maxPane === p.i ? "Restore" : "Maximize"}>{maxPane === p.i ? "❐" : "⬚"}</button>
                {p.i > 0 && <button type="button" className={`cbtn${collapsed.includes(p.i) ? " primary" : ""}`} onClick={() => onCollapse(p.i)} title={collapsed.includes(p.i) ? "Restore" : "Collapse"}>▁</button>}
                {p.i > 0 && <button type="button" className="cbtn" onClick={() => onDelete(p.i)} title="Delete pane">✕</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Modal>
  );
}

interface Insight {
  k: string;
  v: string;
  tone?: "up" | "down" | "";
}

/** A technical read of daily candles. Pure — unit-testable. */
export function computeInsights(c: OhlcvCandle[]): Insight[] {
  const n = c.length;
  if (n < 30) return [];
  const cl = c.map((k) => k.close);
  const last = cl[n - 1];
  const out: Insight[] = [];
  const pct = (a: number, b: number) => ((a - b) / b) * 100;
  const ma = (len: number, f: typeof sma) => f(cl, len)[n - 1];
  const above = [20, 50, 200].map((l) => ({ l, v: ma(l, sma) })).filter((x) => x.v != null) as { l: number; v: number }[];
  if (above.length) {
    const up = above.filter((x) => last > x.v).map((x) => x.l);
    const tone = up.length === above.length ? "up" : up.length === 0 ? "down" : "";
    out.push({ k: "Trend", v: up.length === above.length ? `Above its ${above.map((x) => x.l).join(" / ")}-day averages — uptrend` : up.length === 0 ? `Below its ${above.map((x) => x.l).join(" / ")}-day averages — downtrend` : `Mixed: above ${up.join(" / ") || "none"}, below the rest`, tone });
  }
  const e50 = ma(50, ema), e200 = ma(200, ema);
  if (e50 != null && e200 != null) out.push({ k: "50 / 200 EMA", v: e50 > e200 ? "Golden alignment (50 above 200)" : "Death alignment (50 below 200)", tone: e50 > e200 ? "up" : "down" });
  const r = rsi(cl, 14)[n - 1];
  if (r != null) out.push({ k: "RSI (14)", v: `${r.toFixed(1)} — ${r >= 70 ? "overbought" : r <= 30 ? "oversold" : r >= 50 ? "bullish momentum" : "bearish momentum"}`, tone: r >= 50 ? "up" : "down" });
  const m = macd(cl, 12, 26, 9);
  const h = m.histogram[n - 1], hp = m.histogram[n - 2];
  if (h != null && hp != null) out.push({ k: "MACD", v: `${h >= 0 ? "Above" : "Below"} signal, histogram ${h > hp ? "rising" : "falling"}`, tone: h >= 0 ? "up" : "down" });
  const yr = c.slice(-252);
  const hi = Math.max(...yr.map((k) => k.high)), lo = Math.min(...yr.map((k) => k.low));
  if (hi > lo) out.push({ k: "52-week range", v: `${((100 * (last - lo)) / (hi - lo)).toFixed(0)}% of the way from low ${lo.toFixed(2)} to high ${hi.toFixed(2)} (${pct(last, hi).toFixed(1)}% from the high)`, tone: last >= (hi + lo) / 2 ? "up" : "down" });
  const a = atr(c, 14)[n - 1];
  if (a != null) {
    const atrPct = (a / last) * 100;
    const hist = atr(c, 14).slice(-120).filter((x): x is number => x != null).map((x, i, arr) => x / (c[n - arr.length + i]?.close || last));
    const rank = hist.length ? hist.filter((x) => x <= a / last).length / hist.length : 0.5;
    out.push({ k: "Volatility", v: `ATR ${a.toFixed(2)} (${atrPct.toFixed(2)}% of price) — ${rank > 0.8 ? "high" : rank < 0.2 ? "low (compression)" : "normal"} vs the last 6 months` });
  }
  const v20 = sma(c.map((k) => k.volume), 20)[n - 2];
  if (v20) {
    const rv = c[n - 1].volume / v20;
    out.push({ k: "Volume", v: `${rv.toFixed(2)}× the 20-day average${rv >= 2 ? " — unusual activity" : rv < 0.6 ? " — quiet" : ""}`, tone: rv >= 1.5 ? "up" : "" });
  }
  const ret = (d: number) => (n > d ? pct(last, cl[n - 1 - d]) : null);
  const perf = [["1W", 5], ["1M", 21], ["3M", 63], ["1Y", 252]].map(([l, d]) => [l, ret(d as number)] as const).filter((x) => x[1] != null);
  if (perf.length) out.push({ k: "Performance", v: perf.map(([l, v]) => `${l} ${(v as number) >= 0 ? "+" : ""}${(v as number).toFixed(1)}%`).join(" · "), tone: (perf[perf.length - 1][1] as number) >= 0 ? "up" : "down" });
  const p = c[n - 2];
  if (p) {
    const pp = (p.high + p.low + p.close) / 3;
    out.push({ k: "Pivots (prev. day)", v: `S1 ${(2 * pp - p.high).toFixed(2)} · P ${pp.toFixed(2)} · R1 ${(2 * pp - p.low).toFixed(2)}` });
  }
  const pats = candlePatterns(c.slice(-5)).filter((x) => x.i >= 3);
  if (pats.length) out.push({ k: "Latest candles", v: pats.map((x) => x.name).join(", "), tone: pats[pats.length - 1].bias > 0 ? "up" : pats[pats.length - 1].bias < 0 ? "down" : "" });
  const tr = technicalRating(c);
  if (tr) out.push({ k: "Technical rating", v: `${tr.rating} (${tr.votes.buy} buy · ${tr.votes.neutral} neutral · ${tr.votes.sell} sell)`, tone: tr.score > 0.1 ? "up" : tr.score < -0.1 ? "down" : "" });
  return out;
}

interface Announcement {
  id: number;
  headline: string;
  filed_at: string | null;
}

export function InsightsDialog({ symbol, name, onClose }: { symbol: string; name: string; onClose: () => void }) {
  const [rows, setRows] = useState<Insight[] | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  const [news, setNews] = useState<Announcement[]>([]);
  useEffect(() => {
    const now = Math.floor(Date.now() / 1000);
    const qs = new URLSearchParams({ symbol, resolution: "D", from: String(now - 420 * 86400), to: String(now) });
    void fetch(`/api/market/history?${qs.toString()}`)
      .then((r) => r.json())
      .then((j: { ok?: boolean; reason?: string | null; candles?: number[][] }) => {
        if (!j.ok) {
          setReason(j.reason ?? "daily history unavailable");
          setRows([]);
          return;
        }
        const c = (j.candles ?? []).filter((r) => r.length >= 5).map((r) => ({ time: r[0], open: r[1], high: r[2], low: r[3], close: r[4], volume: r[5] ?? 0 }));
        setRows(computeInsights(c));
      })
      .catch(() => {
        setReason("daily history request failed");
        setRows([]);
      });
    const tk = symbol.includes(":") ? symbol.split(":")[1].replace(/-(EQ|BE|INDEX)$/, "") : symbol;
    void fetch(`/api/announcements/recent?symbol=${encodeURIComponent(tk)}&limit=6`)
      .then((r) => (r.ok ? r.json() : []))
      .then((j: Announcement[]) => setNews(Array.isArray(j) ? j.slice(0, 6) : []))
      .catch(() => undefined);
  }, [symbol]);
  return (
    <Modal title={`Insights · ${name}`} onClose={onClose} width={560} testid="insights">
      {rows === null && <div className="hint">reading daily bars…</div>}
      {rows !== null && rows.length === 0 && <div className="hint">{reason ?? "Not enough daily history for insights."}</div>}
      {rows && rows.length > 0 && (
        <div className="insights-grid">
          {rows.map((r) => (
            <div key={r.k} className="insight">
              <span className="hint">{r.k}</span>
              <span className={r.tone ?? ""}>{r.v}</span>
            </div>
          ))}
        </div>
      )}
      {news.length > 0 && (
        <>
          <div className="cform-sec-title">Recent filings</div>
          {news.map((a) => (
            <div key={a.id} className="insight">
              <span className="hint">{a.filed_at ? new Date(a.filed_at).toLocaleDateString("en-IN", { day: "2-digit", month: "short" }) : ""}</span>
              <span>{a.headline}</span>
            </div>
          ))}
        </>
      )}
      <div className="hint insights-note">Computed from daily candles and exchange filings — not investment advice.</div>
    </Modal>
  );
}

const WHATS_NEW: [string, string[]][] = [
  ["Chart", [
    "≈140 indicators incl. the Fyers desk set (CPR D / W / M, ORB, ATR trailing stop, Chandelier, KAMA, Jurik, candlestick patterns, RSI divergence, OI)",
    "Shaded clouds and band backgrounds; plot type per output; pattern / fractal markers",
    "Legend More menu: move to pane, pin to scale, visual order, copy, About; Manage panes",
    "Lock price to bar ratio, raw-price label in % mode, executions on bars, ticks P&L",
  ]],
  ["Drawing", [
    "Pin, arrow marks in all four directions, stickers and icon glyphs, drag-to-zoom",
    "Named drawing templates, visibility ranges per timeframe, copy / paste between charts",
  ]],
  ["Desk", [
    "Market depth ladder, time & sales, futures chain, options strategy builder",
    "Fyers live view of the whole account; light theme; 4 / 5 / 6 / 7 / 8-chart layouts",
  ]],
];

export function WhatsNewDialog({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="What's new" onClose={onClose} width={520} testid="whats-new-dialog">
      {WHATS_NEW.map(([sec, items]) => (
        <div key={sec}>
          <div className="cform-sec-title">{sec}</div>
          <ul className="whats-new">{items.map((t) => <li key={t}>{t}</li>)}</ul>
        </div>
      ))}
    </Modal>
  );
}
