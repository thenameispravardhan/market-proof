// ChartDialogs — symbol search, go to date, the command palette (Quick
// Search), keyboard shortcuts, the alert editor, and drawing settings.

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import type { InstrumentHit, SearchResponse } from "../../types";
import { Check, ColorInput, Modal, Num, Row, Sel } from "./chartUi";
import { COND_LABEL, TRIGGER_LABEL, type AlertCond, type AlertItem, type AlertTrigger } from "./alerts";
import { TOOL_BY_ID, styleOf, type Drawing, type DrawingDeps, type DrawingStyle, type Level } from "./drawings";
import { VIS_GROUPS } from "./indicatorCatalog";

// ---------------------------------------------------------------------------
// Symbol search
// ---------------------------------------------------------------------------

const SEGMENTS: { id: string; label: string; seg: string | null }[] = [
  { id: "all", label: "All", seg: null },
  { id: "stocks", label: "Stocks", seg: "EQ" },
  { id: "fno", label: "Futures & options", seg: "FO" },
  { id: "indices", label: "Indices", seg: "INDEX" },
  { id: "commodity", label: "Commodities", seg: "COM" },
  { id: "currency", label: "Currency", seg: "CD" },
];

const TYPE_BADGE: Record<string, string> = { EQ: "stock", FUT: "futures", CE: "call", PE: "put", IND: "index" };

export function SymbolSearchDialog({
  initial = "",
  title = "Symbol Search",
  recent,
  onPick,
  onClose,
}: {
  initial?: string;
  title?: string;
  recent: InstrumentHit[];
  onPick: (h: InstrumentHit) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState(initial);
  const [seg, setSeg] = useState("all");
  const [exch, setExch] = useState("all");
  const [hits, setHits] = useState<InstrumentHit[]>([]);
  const [busy, setBusy] = useState(false);
  const [hl, setHl] = useState(0);
  useEffect(() => {
    const query = q.trim();
    if (!query) {
      setHits([]);
      return;
    }
    setBusy(true);
    const s = SEGMENTS.find((x) => x.id === seg)?.seg;
    const h = setTimeout(() => {
      void api
        .get<SearchResponse>(`/api/search/symbols?q=${encodeURIComponent(query)}&limit=50${s ? `&segment=${s}` : ""}`)
        .then((r) => setHits(r.hits ?? []))
        .catch(() => setHits([]))
        .finally(() => setBusy(false));
    }, 200);
    return () => clearTimeout(h);
  }, [q, seg]);
  const shown = (q.trim() ? hits : recent).filter((h) => exch === "all" || h.exchange === exch);
  useEffect(() => setHl(0), [q, seg, exch]);
  return (
    <Modal title={title} onClose={onClose} width={620} testid="symbol-search" className="sym-search">
      <input
        className="chart-menu-input sym-input"
        value={q}
        autoFocus
        placeholder="Search — RELIANCE, NIFTY, BANKNIFTY…"
        aria-label="Search symbol"
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setHl((i) => Math.min(shown.length - 1, i + 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setHl((i) => Math.max(0, i - 1)); }
          else if (e.key === "Enter" && shown[hl]) { onPick(shown[hl]); onClose(); }
        }}
        data-testid="symbol-search-input"
      />
      <div className="sym-filters">
        {SEGMENTS.map((s) => (
          <button key={s.id} type="button" className={`chip${seg === s.id ? " on" : ""}`} onClick={() => setSeg(s.id)}>{s.label}</button>
        ))}
        <span className="grow" />
        <Sel value={exch} options={[{ v: "all", l: "All exchanges" }, { v: "NSE", l: "NSE" }, { v: "BSE", l: "BSE" }, { v: "MCX", l: "MCX" }]} onChange={setExch} ariaLabel="Exchange" />
      </div>
      <div className="sym-list">
        {!q.trim() && recent.length > 0 && <div className="sym-head">Recently searched</div>}
        {busy && <div className="hint">searching…</div>}
        {!busy && q.trim() && shown.length === 0 && <div className="hint">No symbols match "{q}".</div>}
        {shown.map((h, i) => (
          <button
            key={h.symbol}
            type="button"
            className={`sym-row${i === hl ? " hl" : ""}`}
            onMouseEnter={() => setHl(i)}
            onClick={() => { onPick(h); onClose(); }}
            data-testid={`symbol-search-row-${h.symbol}`}
          >
            <span className="sym">{h.short_name}</span>
            <span className="desc">{h.display}</span>
            <span className="badge neutral">{TYPE_BADGE[h.instrument_type] ?? h.instrument_type}</span>
            <span className="exch">{h.exchange}</span>
          </button>
        ))}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Go to date
// ---------------------------------------------------------------------------

/** "YYYY-MM-DD" + "HH:mm" (exchange wall clock) → chart time. */
export function wallToChart(date: string, time = "00:00"): number | null {
  const t = Date.parse(`${date}T${time || "00:00"}:00Z`);
  return Number.isFinite(t) ? Math.floor(t / 1000) : null;
}

export function chartToWall(t: number): { date: string; time: string } {
  const iso = new Date(t * 1000).toISOString();
  return { date: iso.slice(0, 10), time: iso.slice(11, 16) };
}

export function GoToDialog({ onGo, onClose, last }: { onGo: (r: { at?: number; from?: number; to?: number }) => void; onClose: () => void; last: number | null }) {
  const today = chartToWall(last ?? Math.floor(Date.now() / 1000) + 19800).date;
  const [tab, setTab] = useState("date");
  const [date, setDate] = useState(today);
  const [time, setTime] = useState("");
  const [from, setFrom] = useState(today);
  const [to, setTo] = useState(today);
  const [fromTime, setFromTime] = useState("");
  const [toTime, setToTime] = useState("");
  const go = () => {
    if (tab === "date") {
      const at = wallToChart(date, time || "09:15");
      if (at !== null) onGo({ at });
    } else {
      const f = wallToChart(from, fromTime || "00:00"), t = wallToChart(to, toTime || "23:59");
      if (f !== null && t !== null && t > f) onGo({ from: f, to: t });
    }
    onClose();
  };
  return (
    <Modal
      title="Go to"
      onClose={onClose}
      width={420}
      testid="goto-dialog"
      footer={<><span className="grow" /><button type="button" className="cbtn" onClick={onClose}>Cancel</button><button type="button" className="cbtn primary" onClick={go} data-testid="goto-go">Go to</button></>}
    >
      <div className="sym-filters">
        <button type="button" className={`chip${tab === "date" ? " on" : ""}`} onClick={() => setTab("date")}>Date</button>
        <button type="button" className={`chip${tab === "range" ? " on" : ""}`} onClick={() => setTab("range")}>Custom range</button>
      </div>
      {tab === "date" ? (
        <>
          <Row label="Date"><input type="date" value={date} onChange={(e) => setDate(e.target.value)} aria-label="Date" /></Row>
          <Row label="Time (optional)"><input type="time" value={time} onChange={(e) => setTime(e.target.value)} aria-label="Time" /></Row>
        </>
      ) : (
        <>
          <Row label="From">
            <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} aria-label="From" />
            <input type="time" value={fromTime} onChange={(e) => setFromTime(e.target.value)} aria-label="From time" title="Optional time" />
          </Row>
          <Row label="To">
            <input type="date" value={to} onChange={(e) => setTo(e.target.value)} aria-label="To" />
            <input type="time" value={toTime} onChange={(e) => setToTime(e.target.value)} aria-label="To time" title="Optional time" />
          </Row>
        </>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Command palette
// ---------------------------------------------------------------------------

export interface Command {
  id: string;
  label: string;
  group: string;
  hint?: string;
  run: () => void;
}

export function CommandPalette({ commands, onClose }: { commands: Command[]; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [hl, setHl] = useState(0);
  const list = useMemo(() => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    return commands.filter((c) => words.every((w) => `${c.label} ${c.group}`.toLowerCase().includes(w))).slice(0, 80);
  }, [q, commands]);
  useEffect(() => setHl(0), [q]);
  const listRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    listRef.current?.querySelector(".hl")?.scrollIntoView?.({ block: "nearest" });
  }, [hl]);
  return (
    <Modal title="Quick Search" onClose={onClose} width={560} testid="command-palette" className="cmd-pal">
      <input
        className="chart-menu-input"
        autoFocus
        value={q}
        placeholder="Search actions, tools, indicators, settings…"
        aria-label="Search commands"
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setHl((i) => Math.min(list.length - 1, i + 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setHl((i) => Math.max(0, i - 1)); }
          else if (e.key === "Enter" && list[hl]) { onClose(); list[hl].run(); }
        }}
      />
      <div className="cmd-list" ref={listRef}>
        {list.length === 0 && <div className="hint">Nothing matches.</div>}
        {list.map((c, i) => (
          <button key={c.id} type="button" className={`cmd-row${i === hl ? " hl" : ""}`} onMouseEnter={() => setHl(i)} onClick={() => { onClose(); c.run(); }}>
            <span>{c.label}</span>
            <span className="cmd-group">{c.hint ? <span className="kbd">{c.hint}</span> : null}{c.group}</span>
          </button>
        ))}
      </div>
    </Modal>
  );
}

export const SHORTCUTS: [string, string][] = [
  ["Alt + T", "Trend line"],
  ["Alt + H", "Horizontal line at the cursor"],
  ["Alt + V", "Vertical line"],
  ["Alt + J", "Horizontal ray"],
  ["Alt + C", "Cross line"],
  ["Alt + Shift + R", "Rectangle"],
  ["Ctrl + Shift + S", "Copy chart image"],
  ["Alt + F", "Fib retracement"],
  ["Alt + A", "Add alert at the cursor"],
  ["Alt + I", "Invert scale"],
  ["Alt + L", "Log scale"],
  ["Alt + P", "Percent scale"],
  ["Alt + R", "Reset chart view"],
  ["Alt + W", "Add symbol to watchlist"],
  ["Alt + Enter", "Maximize / restore chart"],
  ["Shift + T", "Order ticket at the cursor price"],
  ["Alt + Shift + B", "Buy / sell order at the cursor price"],
  ["Ctrl + Z / Ctrl + Y", "Undo / redo"],
  ["Ctrl + C / Ctrl + V", "Copy / paste the selected drawing"],
  ["Ctrl + S", "Save layout"],
  ["Ctrl + K", "Quick search"],
  ["/", "Indicators"],
  ["Type letters", "Symbol search"],
  ["Type digits or ,", "Change interval"],
  ["← / →", "Scroll one bar (Shift: ten)"],
  ["↑ / ↓", "Zoom in / out"],
  ["Shift + drag / Shift + click", "Snap the drawing to 45°"],
  ["Ctrl + drag a drawing", "Clone it"],
  ["Delete", "Remove the selected drawing"],
  ["Esc", "Cancel drawing / deselect / close"],
  ["Shift + F", "Fullscreen"],
  ["Ctrl + Alt + S", "Snapshot"],
  ["?", "This list"],
];

export function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="Keyboard shortcuts" onClose={onClose} width={460} testid="shortcuts">
      <table className="kbd-table">
        <tbody>
          {SHORTCUTS.map(([k, d]) => (
            <tr key={k}><td><span className="kbd">{k}</span></td><td>{d}</td></tr>
          ))}
        </tbody>
      </table>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Alerts
// ---------------------------------------------------------------------------

export interface AlertTarget {
  id: string;
  label: string;
  /** Current value, for display. */
  value: number | null;
}

/** Epoch ms ↔ datetime-local string in the exchange clock (IST). */
function msToLocal(ms: number): string {
  return new Date(ms + 19800 * 1000).toISOString().slice(0, 16);
}
function localToMs(s: string): number | null {
  const t = Date.parse(`${s}:00Z`);
  return Number.isFinite(t) ? t - 19800 * 1000 : null;
}

export function AlertDialog({
  alert,
  symbolName,
  targets,
  fmt,
  onSave,
  onClose,
}: {
  alert: AlertItem;
  symbolName: string;
  targets: AlertTarget[];
  fmt: (n: number) => string;
  onSave: (a: AlertItem) => void;
  onClose: () => void;
}) {
  const [a, setA] = useState(alert);
  const set = (patch: Partial<AlertItem>) => setA((x) => ({ ...x, ...patch }));
  const channel = a.cond === "enter" || a.cond === "exit";
  const tgt = targets.find((t) => t.id === a.target);
  return (
    <Modal
      title={`Create alert on ${symbolName}`}
      onClose={onClose}
      width={460}
      testid="alert-dialog"
      footer={<><span className="grow" /><button type="button" className="cbtn" onClick={onClose}>Cancel</button><button type="button" className="cbtn primary" onClick={() => { onSave({ ...a, targetLabel: tgt?.label ?? a.targetLabel, active: true }); onClose(); }} data-testid="alert-save">Create</button></>}
    >
      <Row label="Condition"><span className="hint">{symbolName} price</span></Row>
      <Row label="">
        <Sel value={a.cond} options={(Object.keys(COND_LABEL) as AlertCond[]).filter((c) => a.target === "value" || (c !== "enter" && c !== "exit")).map((c) => ({ v: c, l: COND_LABEL[c] }))} onChange={(v) => set({ cond: v })} ariaLabel="Condition" />
      </Row>
      <Row label="">
        <Sel value={a.target} options={[{ v: "value", l: "Value" }, ...targets.map((t) => ({ v: t.id, l: t.label }))]} onChange={(v) => set({ target: v, cond: v !== "value" && (a.cond === "enter" || a.cond === "exit") ? "cross" : a.cond })} ariaLabel="Target" />
        {a.target === "value" ? (
          <>
            <Num value={a.value} step={0.05} width={100} onChange={(v) => set({ value: v })} ariaLabel="Value" />
            {channel && <Num value={a.value2 ?? a.value} step={0.05} width={100} onChange={(v) => set({ value2: v })} ariaLabel="Upper value" />}
          </>
        ) : (
          <span className="hint">now {tgt?.value != null ? fmt(tgt.value) : "—"}</span>
        )}
      </Row>
      <Row label="Trigger">
        <Sel value={a.trigger} options={(Object.keys(TRIGGER_LABEL) as AlertTrigger[]).map((t) => ({ v: t, l: TRIGGER_LABEL[t] }))} onChange={(v) => set({ trigger: v })} ariaLabel="Trigger" />
      </Row>
      <Row label="Expiration">
        <label className="cform-check"><input type="checkbox" checked={a.expires === null} onChange={(e) => set({ expires: e.target.checked ? null : Date.now() + 30 * 86400 * 1000 })} />Open-ended</label>
        {a.expires !== null && <input type="datetime-local" value={msToLocal(a.expires)} onChange={(e) => set({ expires: localToMs(e.target.value) })} aria-label="Expires" />}
      </Row>
      <Row label="Message"><textarea className="cform-text" rows={2} value={a.message} placeholder={`${symbolName} ${COND_LABEL[a.cond]} …`} onChange={(e) => set({ message: e.target.value })} aria-label="Message" /></Row>
      <div className="cform-sec-title">Notifications</div>
      <Check label="Show popup" checked={a.popup} onChange={(v) => set({ popup: v })} />
      <Check label="Play sound" checked={a.sound} onChange={(v) => set({ sound: v })} />
      <Check label="Browser notification" checked={a.notify} onChange={(v) => set({ notify: v })} />
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Drawing settings
// ---------------------------------------------------------------------------

const DASHES = [{ v: 0 as const, l: "Solid" }, { v: 2 as const, l: "Dashed" }, { v: 1 as const, l: "Dotted" }];
const EXTENDABLE = new Set(["trend", "info", "channel", "regression", "flattop", "fib", "fibext", "fibchannel"]);
const TEXT_TYPES = new Set(["trend", "hline", "vline", "rect", "arrowmarker", "arrowup", "arrowdown"]);

export function DrawingSettingsDialog({
  drawing,
  deps,
  onChange,
  onClose,
  fmt,
}: {
  drawing: Drawing;
  deps: Pick<DrawingDeps, "toolDefaults" | "lineColor"> & Partial<Pick<DrawingDeps, "candles" | "timeToLogical">>;
  onChange: (d: Drawing) => void;
  onClose: (commit: boolean) => void;
  fmt: (n: number) => string;
}) {
  const tool = TOOL_BY_ID.get(drawing.type);
  const [orig] = useState(drawing);
  const d = drawing;
  const s = styleOf(d, deps);
  const hasText = !!tool?.text || TEXT_TYPES.has(d.type);
  const isPos = d.type === "long" || d.type === "short";
  const tabs = [
    ...(isPos ? [{ id: "inputs", label: "Inputs" }] : []),
    { id: "style", label: "Style" },
    ...(hasText ? [{ id: "text", label: "Text" }] : []),
    ...(!tool?.screen ? [{ id: "coords", label: "Coordinates" }] : []),
    { id: "visibility", label: "Visibility" },
  ];
  const [tab, setTab] = useState(tabs[0].id);
  const setStyle = (patch: Partial<DrawingStyle>) => onChange({ ...d, style: { ...(d.style ?? {}), ...patch } });
  const setLevel = (i: number, patch: Partial<Level>) => setStyle({ levels: (s.levels ?? []).map((l, j) => (j === i ? { ...l, ...patch } : l)) });
  const line = s.color || deps.lineColor();
  return (
    <Modal
      title={d.name || tool?.label || "Drawing"}
      onClose={() => { onChange(orig); onClose(false); }}
      width={480}
      tabs={tabs}
      tab={tab}
      onTab={setTab}
      testid="drawing-settings"
      footer={<><span className="grow" /><button type="button" className="cbtn" onClick={() => { onChange(orig); onClose(false); }}>Cancel</button><button type="button" className="cbtn primary" onClick={() => onClose(true)} data-testid="drawing-settings-ok">Ok</button></>}
    >
      {tab === "inputs" && isPos && (
        <>
          <Row label="Account size"><Num value={d.data?.account ?? 100000} min={0} step={1000} width={110} onChange={(v) => onChange({ ...d, data: { ...d.data, account: v } })} ariaLabel="Account size" /></Row>
          <Row label="Risk">
            <Sel value={d.data?.riskMode ?? "pct"} options={[{ v: "pct", l: "% of account" }, { v: "amount", l: "Amount (₹)" }]} onChange={(v) => onChange({ ...d, data: { ...d.data, riskMode: v as "pct" | "amount" } })} ariaLabel="Risk mode" />
            {(d.data?.riskMode ?? "pct") === "pct" ? (
              <Num value={d.data?.risk ?? 1} min={0} max={100} step={0.25} onChange={(v) => onChange({ ...d, data: { ...d.data, risk: v } })} ariaLabel="Risk" />
            ) : (
              <Num value={d.data?.riskAmount ?? 1000} min={0} step={100} width={100} onChange={(v) => onChange({ ...d, data: { ...d.data, riskAmount: v } })} ariaLabel="Risk amount" />
            )}
          </Row>
          <Row label="Lot size"><Num value={d.data?.lot ?? 1} min={1} onChange={(v) => onChange({ ...d, data: { ...d.data, lot: Math.round(v) } })} ariaLabel="Lot size" /></Row>
          <Row label="Quantity" hint="0 = sized from account × risk"><Num value={d.data?.qty ?? 0} min={0} onChange={(v) => onChange({ ...d, data: { ...d.data, qty: Math.round(v) } })} ariaLabel="Quantity" /></Row>
          {(["Entry price", "Target price", "Stop price"] as const).map((lbl, i) => (
            <Row key={lbl} label={lbl}>
              <Num value={d.points[i]?.price ?? 0} step={0.05} width={110} onChange={(v) => onChange({ ...d, points: d.points.map((p, j) => (j === i ? { ...p, price: v } : p)) })} ariaLabel={lbl} />
            </Row>
          ))}
        </>
      )}
      {tab === "style" && (
        <>
          <Row label="Line">
            <ColorInput value={s.color} fallback={deps.lineColor()} onChange={(c) => setStyle({ color: c })} />
            <Sel value={s.width} options={[1, 2, 3, 4, 6, 10].map((w) => ({ v: w, l: `${w}px` }))} onChange={(w) => setStyle({ width: w })} ariaLabel="Line width" />
            <Sel value={s.dash} options={DASHES} onChange={(v) => setStyle({ dash: v })} ariaLabel="Line style" />
          </Row>
          <Check label="Background" checked={s.fill} onChange={(v) => setStyle({ fill: v })}>
            <ColorInput value={s.fillColor} fallback={line} opacity={s.fillOpacity} onOpacity={(o) => setStyle({ fillOpacity: o })} onChange={(c) => setStyle({ fillColor: c })} />
          </Check>
          {EXTENDABLE.has(d.type) && (
            <>
              <Check label="Extend left" checked={s.extendLeft} onChange={(v) => setStyle({ extendLeft: v })} />
              <Check label="Extend right" checked={s.extendRight} onChange={(v) => setStyle({ extendRight: v })} />
            </>
          )}
          <Check label="Labels" checked={s.labels} onChange={(v) => setStyle({ labels: v })} />
          {s.levels && (
            <>
              <div className="cform-sec-title">Levels</div>
              <div className="lvl-grid">
                {s.levels.map((l, i) => (
                  <span key={i} className="lvl-cell">
                    <input type="checkbox" checked={l.on} onChange={(e) => setLevel(i, { on: e.target.checked })} aria-label={`Level ${l.v}`} />
                    <Num value={l.v} step={0.001} width={64} onChange={(v) => setLevel(i, { v })} ariaLabel="Level value" />
                    <ColorInput value={l.color} fallback={l.color} onChange={(c) => setLevel(i, { color: c || l.color })} />
                  </span>
                ))}
              </div>
            </>
          )}
        </>
      )}
      {tab === "text" && (
        <>
          <textarea className="cform-text" rows={4} value={d.text ?? ""} onChange={(e) => onChange({ ...d, text: e.target.value })} aria-label="Text" data-testid="drawing-text" />
          <Row label="Font">
            <ColorInput value={s.textColor} fallback={line} onChange={(c) => setStyle({ textColor: c })} />
            <Sel value={s.fontSize} options={[10, 11, 12, 14, 16, 20, 24, 28, 32, 40].map((n) => ({ v: n, l: String(n) }))} onChange={(v) => setStyle({ fontSize: v })} ariaLabel="Font size" />
            <label className="cform-check"><input type="checkbox" checked={s.bold} onChange={(e) => setStyle({ bold: e.target.checked })} /><b>B</b></label>
            <label className="cform-check"><input type="checkbox" checked={s.italic} onChange={(e) => setStyle({ italic: e.target.checked })} /><i>I</i></label>
          </Row>
        </>
      )}
      {tab === "coords" &&
        d.points.map((p, i) => {
          const w = chartToWall(p.time);
          const bars = deps.candles?.() ?? [];
          const bar = deps.timeToLogical?.(p.time);
          const timeOfBar = (k: number): number | null => {
            if (!bars.length) return null;
            if (k >= 0 && k < bars.length) return bars[k].time;
            const step = bars.length > 1 ? bars[bars.length - 1].time - bars[bars.length - 2].time : 60;
            return k < 0 ? bars[0].time + k * step : bars[bars.length - 1].time + (k - bars.length + 1) * step;
          };
          return (
            <Row key={i} label={`#${i + 1}`}>
              <Num value={Math.round(p.price * 100) / 100} step={0.05} width={100} onChange={(v) => onChange({ ...d, points: d.points.map((q, j) => (j === i ? { ...q, price: v } : q)) })} ariaLabel={`Point ${i + 1} price`} />
              {bar != null && (
                <Num
                  value={Math.round(bar)}
                  step={1}
                  width={70}
                  onChange={(v) => {
                    const t = timeOfBar(Math.round(v));
                    if (t !== null) onChange({ ...d, points: d.points.map((q, j) => (j === i ? { ...q, time: t } : q)) });
                  }}
                  ariaLabel={`Point ${i + 1} bar`}
                />
              )}
              <input
                type="datetime-local"
                value={`${w.date}T${w.time}`}
                aria-label={`Point ${i + 1} time`}
                onChange={(e) => {
                  const t = wallToChart(e.target.value.slice(0, 10), e.target.value.slice(11, 16));
                  if (t !== null) onChange({ ...d, points: d.points.map((q, j) => (j === i ? { ...q, time: t } : q)) });
                }}
              />
              <span className="hint">{fmt(p.price)}</span>
            </Row>
          );
        })}
      {tab === "visibility" &&
        VIS_GROUPS.map((g) => {
          const raw = d.vis?.[g.id];
          const v = raw && typeof raw === "object" ? raw : { on: raw !== false, min: 1, max: g.max };
          const put = (patch: Partial<typeof v>) => {
            const next = { ...v, ...patch };
            const whole = next.min <= 1 && next.max >= g.max;
            onChange({ ...d, vis: { ...(d.vis ?? {}), [g.id]: whole ? next.on : next } });
          };
          return (
            <div key={g.id} className="cform-row">
              <label className="cform-check">
                <input type="checkbox" checked={v.on} onChange={(e) => put({ on: e.target.checked })} />
                {g.label}
              </label>
              <span className="cform-ctl">
                <Num value={v.min} min={1} max={g.max} width={56} onChange={(n) => put({ min: Math.round(n) })} ariaLabel={`${g.label} from`} />
                <span className="hint">–</span>
                <Num value={v.max} min={1} max={g.max} width={56} onChange={(n) => put({ max: Math.round(n) })} ariaLabel={`${g.label} to`} />
              </span>
            </div>
          );
        })}
    </Modal>
  );
}
