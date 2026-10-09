// IndicatorDialogs — the "Indicators & Strategies" picker and the
// per-indicator settings dialog (Inputs / Style / Visibility).

import { useMemo, useRef, useState } from "react";
import {
  CATEGORIES,
  INDICATORS,
  INDICATOR_BY_TYPE,
  PLOT_KINDS,
  visibleOnInterval,
  SOURCES,
  VIS_GROUPS,
  defaultFills,
  defaultInputs,
  defaultPlots,
  type IndicatorInstance,
  type InputValue,
  type PlotKind,
} from "./indicatorCatalog";
import { Check, ColorInput, Modal, Num, Row, Sel, useOutside } from "./chartUi";
import type { InstrumentHit } from "../../types";

export interface StrategyItem {
  id: number;
  name: string;
  spec: Record<string, unknown>;
}

export interface IndicatorTemplate {
  name: string;
  items: IndicatorInstance[];
  /** Saved with "Remember symbol": applying it switches the chart there. */
  symbol?: { symbol: string; name: string; hit?: InstrumentHit | null };
  /** Saved with "Remember interval". */
  interval?: string;
}

/** "RELIANCE · 15m" — what a template switches the chart to, if anything. */
export function templateScope(t: IndicatorTemplate, ivLabel: (k: string) => string = (k) => k): string {
  return [t.symbol?.name, t.interval ? ivLabel(t.interval) : null].filter(Boolean).join(" · ");
}

export function SaveTemplateDialog({
  symbolLabel,
  intervalLabel,
  existing,
  count,
  onSave,
  onClose,
}: {
  symbolLabel: string;
  intervalLabel: string;
  existing: string[];
  /** Indicators on the chart now: an empty template would only clear charts. */
  count: number;
  onSave: (name: string, withSymbol: boolean, withInterval: boolean) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [withSymbol, setWithSymbol] = useState(false);
  const [withInterval, setWithInterval] = useState(false);
  const n = name.trim();
  const save = () => {
    if (!n || count === 0) return;
    onSave(n, withSymbol, withInterval);
    onClose();
  };
  return (
    <Modal
      title="Save indicator template"
      onClose={onClose}
      width={400}
      testid="ind-template-save"
      footer={<><span className="grow" /><button type="button" className="cbtn" onClick={onClose}>Cancel</button><button type="button" className="cbtn primary" disabled={!n || count === 0} onClick={save} data-testid="ind-template-save-ok">Save</button></>}
    >
      <Row label="Template name">
        <input className="cform-input" autoFocus value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()} aria-label="Template name" data-testid="ind-template-name" />
      </Row>
      <Check label={`Remember symbol (${symbolLabel})`} checked={withSymbol} onChange={setWithSymbol} testid="ind-template-symbol" />
      <Check label={`Remember interval (${intervalLabel})`} checked={withInterval} onChange={setWithInterval} testid="ind-template-interval" />
      {count === 0 ? (
        <div className="hint warn-text">Add some indicators to the chart first: a template saves the ones on it now.</div>
      ) : (
        <div className="hint">Saves the {count} indicator{count === 1 ? "" : "s"} on the chart now.</div>
      )}
      {existing.includes(n) && <div className="hint warn-text">Replaces the existing template "{n}".</div>}
    </Modal>
  );
}

export function IndicatorPicker({
  favorites,
  onFav,
  onAdd,
  onClose,
  strategies,
  onRunStrategy,
  templates,
  onApplyTemplate,
  intraday,
}: {
  favorites: string[];
  onFav: (type: string) => void;
  onAdd: (type: string) => void;
  onClose: () => void;
  strategies: StrategyItem[] | null;
  onRunStrategy: (s: StrategyItem) => void;
  templates: IndicatorTemplate[];
  onApplyTemplate: (t: IndicatorTemplate) => void;
  intraday: boolean;
}) {
  const [q, setQ] = useState("");
  const [tab, setTab] = useState<string>(favorites.length ? "fav" : "all");
  const [added, setAdded] = useState<{ name: string; n: number } | null>(null);
  const list = useMemo(() => {
    const ql = q.trim().toLowerCase();
    let items = INDICATORS;
    if (ql) {
      // best matches first: exact short name ("EMA"), then prefixes, then the rest
      const rank = (d: (typeof INDICATORS)[number]) => {
        const sh = d.short.toLowerCase();
        const nm = d.name.toLowerCase();
        return sh === ql || nm === ql ? 0 : sh.startsWith(ql) || nm.startsWith(ql) ? 1 : 2;
      };
      items = items
        .filter((d) => d.name.toLowerCase().includes(ql) || d.short.toLowerCase().includes(ql) || d.category.toLowerCase().includes(ql))
        .map((d, i) => ({ d, i, r: rank(d) }))
        .sort((a, b) => a.r - b.r || a.i - b.i)
        .map((x) => x.d);
    }
    else if (tab === "fav") items = items.filter((d) => favorites.includes(d.type));
    else if (tab === "desk") items = items.filter((d) => d.desk);
    else if (tab === "all") items = items.filter((d) => !d.desk);
    else items = items.filter((d) => d.category === tab);
    return items;
  }, [q, tab, favorites]);
  const tabs = [
    { id: "fav", label: "★ Favorites" },
    { id: "desk", label: "Fyers indicators" },
    { id: "all", label: "Built-ins" },
    ...CATEGORIES.map((c) => ({ id: c, label: `  ${c}` })),
    { id: "strategies", label: "Strategies" },
    { id: "templates", label: "Templates" },
  ];
  const add = (type: string, name: string) => {
    onAdd(type);
    setAdded((a) => ({ name, n: a?.name === name ? a.n + 1 : 1 }));
  };
  return (
    <Modal title="Indicators, metrics & strategies" onClose={onClose} width={640} tabs={tabs} tab={q.trim() ? "" : tab} onTab={(t) => { setTab(t); setQ(""); }} testid="chart-ind-menu" className="ind-picker">
      <div className="ind-search-bar">
        <input
          className="chart-menu-input ind-search"
          placeholder="Search by name or category · Enter adds the first match"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== "Enter" || !q.trim()) return;
            const first = list.find((d) => !(d.intradayOnly && !intraday));
            if (first) add(first.type, first.name);
          }}
          autoFocus
          aria-label="Search indicators"
        />
        {added && <div className="ind-added" role="status">✓ Added {added.name}{added.n > 1 ? ` ×${added.n}` : ""} to the chart</div>}
      </div>
      {tab === "strategies" && !q.trim() ? (
        <div className="ind-list">
          {strategies === null && <div className="hint">loading…</div>}
          {strategies?.length === 0 && <div className="hint">No saved strategies — build one in Algo Lab.</div>}
          {strategies?.map((s) => (
            <button key={s.id} type="button" className="ind-row" onClick={() => { onRunStrategy(s); onClose(); }}>
              <span className="ind-name">⚙ {s.name}</span>
              <span className="ind-cat">backtest on this symbol, mark its trades</span>
            </button>
          ))}
        </div>
      ) : tab === "templates" && !q.trim() ? (
        <div className="ind-list">
          {templates.length === 0 && <div className="hint">No templates yet — save one from the templates button (▦) in the toolbar.</div>}
          {templates.map((t) => (
            <button key={t.name} type="button" className="ind-row" onClick={() => { onApplyTemplate(t); onClose(); }}>
              <span className="ind-name">▦ {t.name}{templateScope(t) && <span className="hint"> · {templateScope(t)}</span>}</span>
              <span className="ind-cat">{t.items.map((i) => INDICATOR_BY_TYPE.get(i.type)?.short ?? i.type).join(", ")}</span>
            </button>
          ))}
        </div>
      ) : (
        <div className="ind-list">
          {list.length === 0 && <div className="hint">{tab === "fav" && !q.trim() ? "No favorites yet — star an indicator." : "Nothing matches."}</div>}
          {list.map((d) => {
            const off = d.intradayOnly && !intraday;
            return (
              <div key={d.type} className={`ind-row${off ? " off" : ""}`}>
                <button
                  type="button"
                  className={`ind-star${favorites.includes(d.type) ? " on" : ""}`}
                  onClick={() => onFav(d.type)}
                  aria-label={`Favorite ${d.name}`}
                  aria-pressed={favorites.includes(d.type)}
                  title={favorites.includes(d.type) ? "Remove from favorites" : "Add to favorites"}
                >
                  {favorites.includes(d.type) ? "★" : "☆"}
                </button>
                <button
                  type="button"
                  className="ind-name"
                  disabled={off}
                  title={off ? "Intraday charts only" : d.desc}
                  onClick={() => add(d.type, d.name)}
                  data-testid={`chart-ind-${d.type}`}
                >
                  {d.name}
                  {d.isNew && <span className="ind-new">NEW</span>}
                </button>
                <span className="ind-cat">{off ? "intraday only" : d.tool ? "drawing tool" : d.category}</span>
              </div>
            );
          })}
        </div>
      )}
    </Modal>
  );
}

const DASH_OPTS = [
  { v: 0 as const, l: "──── Solid" },
  { v: 2 as const, l: "- - - Dashed" },
  { v: 1 as const, l: "····· Dotted" },
];

/** A text input that follows `value` (so Reset shows the default) and
 *  commits on blur / Enter. */
function TextInput({ value, onCommit, placeholder, ariaLabel }: { value: string; onCommit: (v: string) => void; placeholder?: string; ariaLabel: string }) {
  return (
    <input
      key={value}
      className="cform-input"
      defaultValue={value}
      onBlur={(e) => e.target.value !== value && onCommit(e.target.value)}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      placeholder={placeholder}
      aria-label={ariaLabel}
      spellCheck={false}
    />
  );
}

export function IndicatorSettings({
  inst,
  interval,
  intervalLabel,
  onChange,
  onClose,
  onSaveDefault,
}: {
  inst: IndicatorInstance;
  /** The chart's interval, to say when the Visibility tab hides it there. */
  interval?: string;
  intervalLabel?: string;
  onChange: (i: IndicatorInstance) => void;
  onClose: () => void;
  onSaveDefault: (i: IndicatorInstance) => void;
}) {
  const def = INDICATOR_BY_TYPE.get(inst.type);
  const [orig] = useState(inst);
  const [tab, setTab] = useState(def && def.inputs.length ? "inputs" : "style");
  const [menu, setMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  useOutside(menuRef, menu, () => setMenu(false));
  if (!def) return null;
  // an instance saved before the catalog grew can be short a plot or fill
  const plotStyles = def.plots.map((_, i) => inst.plots[i] ?? defaultPlots(def)[i]);
  const fillStyles = (def.fills ?? []).map((f, k) => inst.fills?.[k] ?? { color: f.color, visible: true });
  const set = (patch: Partial<IndicatorInstance>) => onChange({ ...inst, ...patch });
  const setInput = (k: string, v: InputValue) => set({ inputs: { ...inst.inputs, [k]: v } });
  const setPlot = (i: number, patch: Partial<IndicatorInstance["plots"][number]>) =>
    set({ plots: plotStyles.map((p, j) => (j === i ? { ...p, ...patch } : p)) });
  const setFill = (k: number, patch: Partial<(typeof fillStyles)[number]>) =>
    set({ fills: fillStyles.map((x, j) => (j === k ? { ...x, ...patch } : x)) });
  const cancel = () => {
    onChange(orig);
    onClose();
  };
  const changed = JSON.stringify(inst) !== JSON.stringify(orig);
  const offHere = interval !== undefined && !visibleOnInterval(inst, interval);
  const allOff = VIS_GROUPS.every((g) => inst.vis?.[g.id]?.on === false);
  const tabs = [
    ...(def.inputs.length ? [{ id: "inputs", label: "Inputs" }] : []),
    { id: "style", label: "Style" },
    { id: "visibility", label: "Visibility" },
  ];
  return (
    <Modal
      title={<>{def.name}{!inst.visible ? <span className="hint"> · hidden</span> : offHere ? <span className="hint"> · hidden on {intervalLabel ?? "this interval"}</span> : null}</>}
      onClose={cancel}
      width={680}
      tabs={tabs}
      tab={tab}
      onTab={setTab}
      testid="ind-settings"
      className="ind-settings"
      footer={
        <>
          <div className="chart-menu-wrap" ref={menuRef}>
            <button type="button" className="cbtn" onClick={() => setMenu((m) => !m)} aria-expanded={menu} data-testid="ind-settings-defaults">Defaults ▾</button>
            {menu && (
              <div className="chart-menu cdrop up">
                <button
                  type="button"
                  className="chart-menu-item"
                  onClick={() => {
                    set({ inputs: defaultInputs(def), plots: defaultPlots(def), fills: defaultFills(def), precision: null, labelsOnScale: true, valuesInStatus: true, vis: undefined });
                    setMenu(false);
                  }}
                  data-testid="ind-settings-reset"
                >
                  Reset settings
                </button>
                <button
                  type="button"
                  className="chart-menu-item"
                  onClick={() => {
                    // a default is for new copies, which should show up
                    onSaveDefault({ ...inst, visible: true });
                    setMenu(false);
                  }}
                  data-testid="ind-settings-save-default"
                >
                  Save as default
                </button>
              </div>
            )}
          </div>
          <span className="grow" />
          <button type="button" className="cbtn" onClick={cancel} title={changed ? "Discard changes" : undefined}>Cancel</button>
          <button type="button" className="cbtn primary" onClick={onClose} data-testid="ind-settings-ok">OK</button>
        </>
      }
    >
      {tab === "inputs" &&
        def.inputs.map((i) => (
          <Row key={i.key} label={i.label}>
            {i.type === "source" ? (
              <Sel value={String(inst.inputs[i.key] ?? i.def)} options={SOURCES.map((s) => ({ v: s, l: s }))} onChange={(v) => setInput(i.key, v)} ariaLabel={i.label} />
            ) : i.type === "select" ? (
              <Sel value={String(inst.inputs[i.key] ?? i.def)} options={(i.options ?? []).map((s) => ({ v: s, l: s }))} onChange={(v) => setInput(i.key, v)} ariaLabel={i.label} />
            ) : i.type === "bool" ? (
              <input type="checkbox" checked={(inst.inputs[i.key] ?? i.def) === true} onChange={(e) => setInput(i.key, e.target.checked)} aria-label={i.label} />
            ) : i.type === "symbol" ? (
              <TextInput value={String(inst.inputs[i.key] ?? i.def)} onCommit={(v) => setInput(i.key, v.trim().toUpperCase())} placeholder="EXCHANGE:SYMBOL" ariaLabel={i.label} />
            ) : (
              <Num
                value={Number(inst.inputs[i.key] ?? i.def)}
                min={i.min}
                max={i.max}
                step={i.step ?? (i.type === "int" ? 1 : 0.1)}
                onChange={(v) => setInput(i.key, i.type === "int" ? Math.round(v) : v)}
                ariaLabel={i.label}
              />
            )}
          </Row>
        ))}
      {tab === "style" && (
        <>
          {def.plots.map((p, i) => {
            const st = plotStyles[i];
            const kind: PlotKind = st.kind ?? p.kind ?? "line";
            const bar = kind === "hist" || kind === "columns";
            const marks = p.kind === "marks";
            return (
              <div key={p.key} className={`cform-row ind-style-row${st.visible ? "" : " off"}`}>
                <label className="cform-check">
                  <input type="checkbox" checked={st.visible} onChange={(e) => setPlot(i, { visible: e.target.checked })} aria-label={`Show ${p.label}`} />
                  {p.label}
                </label>
                <span className="cform-ctl">
                  <ColorInput value={st.color} fallback={p.color} onChange={(c) => setPlot(i, { color: c || p.color })} title={`${p.label} color`} />
                  {!marks && (
                    <Sel value={kind} options={PLOT_KINDS} onChange={(k) => setPlot(i, { kind: k === (p.kind ?? "line") ? undefined : k })} ariaLabel={`${p.label} plot type`} />
                  )}
                  {!bar && !marks && (
                    <Sel value={st.width} options={[1, 2, 3, 4].map((w) => ({ v: w, l: `${w}px` }))} onChange={(w) => setPlot(i, { width: w })} ariaLabel={`${p.label} thickness`} />
                  )}
                  {(kind === "line" || kind === "step" || kind === "area") && !marks && (
                    <Sel value={st.dash} options={DASH_OPTS} onChange={(d) => setPlot(i, { dash: d })} ariaLabel={`${p.label} line style`} />
                  )}
                </span>
              </div>
            );
          })}
          {(def.fills ?? []).map((f, k) => {
            const st = fillStyles[k];
            return (
              <div key={f.key} className={`cform-row ind-style-row${st.visible ? "" : " off"}`}>
                <label className="cform-check">
                  <input type="checkbox" checked={st.visible} onChange={(e) => setFill(k, { visible: e.target.checked })} aria-label={`Show ${f.label}`} />
                  {f.label}
                </label>
                <span className="cform-ctl">
                  <ColorInput value={st.color} fallback={f.color} onChange={(c) => setFill(k, { color: c || f.color })} title={`${f.label} color`} />
                </span>
              </div>
            );
          })}
          <div className="cform-sec-title">Outputs</div>
          <Row label="Precision">
            <Sel value={inst.precision ?? -1} options={[{ v: -1, l: "Default" }, ...[0, 1, 2, 3, 4, 5, 6, 7, 8].map((n) => ({ v: n, l: String(n) }))]} onChange={(v) => set({ precision: v < 0 ? null : v })} ariaLabel="Precision" />
          </Row>
          <Check label="Labels on price scale" checked={inst.labelsOnScale} onChange={(v) => set({ labelsOnScale: v })} />
          <Check label="Values in status line" checked={inst.valuesInStatus} onChange={(v) => set({ valuesInStatus: v })} />
        </>
      )}
      {tab === "visibility" && (
        <>
          <div className="hint ind-vis-hint">Show this indicator only on the intervals ticked below.</div>
          {allOff ? (
            <div className="hint warn-text" data-testid="ind-vis-warn">Every interval is unticked, so this indicator never shows.</div>
          ) : offHere ? (
            <div className="hint warn-text" data-testid="ind-vis-warn">Not shown on this chart's interval ({intervalLabel ?? interval}).</div>
          ) : null}
          {VIS_GROUPS.map((g) => {
            const v = inst.vis?.[g.id] ?? { on: true, min: 1, max: g.max };
            const put = (patch: Partial<typeof v>) => {
              const next = { ...v, ...patch };
              // keep the range the right way round: moving one end past the other drags it along
              if (patch.min !== undefined && next.min > next.max) next.max = next.min;
              if (patch.max !== undefined && next.max < next.min) next.min = next.max;
              set({ vis: { ...(inst.vis ?? {}), [g.id]: next } });
            };
            return (
              <div key={g.id} className={`cform-row${v.on ? "" : " off"}`}>
                <label className="cform-check">
                  <input type="checkbox" checked={v.on} onChange={(e) => put({ on: e.target.checked })} aria-label={`Show on ${g.label.toLowerCase()}`} />
                  {g.label}
                </label>
                <span className="cform-ctl">
                  <Num value={v.min} min={1} max={g.max} onChange={(n) => put({ min: Math.round(n) })} width={64} ariaLabel={`${g.label} from`} disabled={!v.on} />
                  <span className="hint">to</span>
                  <Num value={v.max} min={1} max={g.max} onChange={(n) => put({ max: Math.round(n) })} width={64} ariaLabel={`${g.label} to`} disabled={!v.on} />
                </span>
              </div>
            );
          })}
        </>
      )}
    </Modal>
  );
}
