// IndicatorDialogs — the "Indicators & Strategies" picker and the
// per-indicator settings dialog (Inputs / Style / Visibility).

import { useMemo, useState } from "react";
import {
  CATEGORIES,
  INDICATORS,
  INDICATOR_BY_TYPE,
  PLOT_KINDS,
  SOURCES,
  VIS_GROUPS,
  defaultFills,
  defaultInputs,
  defaultPlots,
  type IndicatorInstance,
  type InputValue,
  type PlotKind,
} from "./indicatorCatalog";
import { Check, ColorInput, Modal, Num, Row, Sel } from "./chartUi";

export interface StrategyItem {
  id: number;
  name: string;
  spec: Record<string, unknown>;
}

export interface IndicatorTemplate {
  name: string;
  items: IndicatorInstance[];
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
  const [added, setAdded] = useState<string | null>(null);
  const list = useMemo(() => {
    const ql = q.trim().toLowerCase();
    let items = INDICATORS;
    if (ql) items = items.filter((d) => d.name.toLowerCase().includes(ql) || d.short.toLowerCase().includes(ql) || d.category.toLowerCase().includes(ql));
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
  return (
    <Modal title="Indicators, metrics & strategies" onClose={onClose} width={640} tabs={tabs} tab={q ? "all" : tab} onTab={(t) => { setTab(t); setQ(""); }} testid="chart-ind-menu" className="ind-picker">
      <input className="chart-menu-input ind-search" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} autoFocus aria-label="Search indicators" />
      {added && <div className="ind-added">Added {added}</div>}
      {tab === "strategies" && !q ? (
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
      ) : tab === "templates" && !q ? (
        <div className="ind-list">
          {templates.length === 0 && <div className="hint">No templates yet — save one from the templates button (▦) in the toolbar.</div>}
          {templates.map((t) => (
            <button key={t.name} type="button" className="ind-row" onClick={() => { onApplyTemplate(t); onClose(); }}>
              <span className="ind-name">▦ {t.name}</span>
              <span className="ind-cat">{t.items.map((i) => INDICATOR_BY_TYPE.get(i.type)?.short ?? i.type).join(", ")}</span>
            </button>
          ))}
        </div>
      ) : (
        <div className="ind-list">
          {list.length === 0 && <div className="hint">{tab === "fav" && !q ? "No favorites yet — star an indicator." : "Nothing matches."}</div>}
          {list.map((d) => {
            const off = d.intradayOnly && !intraday;
            return (
              <div key={d.type} className={`ind-row${off ? " off" : ""}`}>
                <button type="button" className={`ind-star${favorites.includes(d.type) ? " on" : ""}`} onClick={() => onFav(d.type)} aria-label={`Favorite ${d.name}`} title="Add to favorites">★</button>
                <button
                  type="button"
                  className="ind-name"
                  disabled={off}
                  title={off ? "Intraday charts only" : d.desc}
                  onClick={() => { onAdd(d.type); setAdded(d.name); }}
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
  { v: 0 as const, l: "──── solid" },
  { v: 2 as const, l: "- - - dashed" },
  { v: 1 as const, l: "····· dotted" },
];

export function IndicatorSettings({
  inst,
  onChange,
  onClose,
  onSaveDefault,
}: {
  inst: IndicatorInstance;
  onChange: (i: IndicatorInstance) => void;
  onClose: () => void;
  onSaveDefault: (i: IndicatorInstance) => void;
}) {
  const def = INDICATOR_BY_TYPE.get(inst.type);
  const [orig] = useState(inst);
  const [tab, setTab] = useState(def && def.inputs.length ? "inputs" : "style");
  const [menu, setMenu] = useState(false);
  if (!def) return null;
  const set = (patch: Partial<IndicatorInstance>) => onChange({ ...inst, ...patch });
  const setInput = (k: string, v: InputValue) => set({ inputs: { ...inst.inputs, [k]: v } });
  const setPlot = (i: number, patch: Partial<IndicatorInstance["plots"][number]>) =>
    set({ plots: inst.plots.map((p, j) => (j === i ? { ...p, ...patch } : p)) });
  const tabs = [
    ...(def.inputs.length ? [{ id: "inputs", label: "Inputs" }] : []),
    { id: "style", label: "Style" },
    { id: "visibility", label: "Visibility" },
  ];
  return (
    <Modal
      title={def.name}
      onClose={() => { onChange(orig); onClose(); }}
      width={580}
      tabs={tabs}
      tab={tab}
      onTab={setTab}
      testid="ind-settings"
      footer={
        <>
          <div className="chart-menu-wrap">
            <button type="button" className="cbtn" onClick={() => setMenu((m) => !m)}>Defaults ▾</button>
            {menu && (
              <div className="chart-menu cdrop up">
                <button
                  type="button"
                  className="chart-menu-item"
                  onClick={() => {
                    set({ inputs: defaultInputs(def), plots: defaultPlots(def), fills: defaultFills(def), precision: null, labelsOnScale: true, valuesInStatus: true, vis: undefined });
                    setMenu(false);
                  }}
                >
                  Reset settings
                </button>
                <button type="button" className="chart-menu-item" onClick={() => { onSaveDefault(inst); setMenu(false); }}>Save as default</button>
              </div>
            )}
          </div>
          <span className="grow" />
          <button type="button" className="cbtn" onClick={() => { onChange(orig); onClose(); }}>Cancel</button>
          <button type="button" className="cbtn primary" onClick={onClose} data-testid="ind-settings-ok">Ok</button>
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
              <input type="checkbox" checked={inst.inputs[i.key] === true} onChange={(e) => setInput(i.key, e.target.checked)} aria-label={i.label} />
            ) : i.type === "symbol" ? (
              <input
                className="cform-input"
                defaultValue={String(inst.inputs[i.key] ?? i.def)}
                onBlur={(e) => setInput(i.key, e.target.value.trim().toUpperCase())}
                onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                placeholder="EXCHANGE:SYMBOL"
                aria-label={i.label}
                spellCheck={false}
              />
            ) : (
              <Num value={Number(inst.inputs[i.key] ?? i.def)} min={i.min} max={i.max} step={i.step ?? (i.type === "int" ? 1 : 0.1)} onChange={(v) => setInput(i.key, i.type === "int" ? Math.round(v) : v)} ariaLabel={i.label} />
            )}
          </Row>
        ))}
      {tab === "style" && (
        <>
          {def.plots.map((p, i) => {
            const st = inst.plots[i];
            const kind: PlotKind = st.kind ?? p.kind ?? "line";
            const bar = kind === "hist" || kind === "columns";
            return (
              <div key={p.key} className="cform-row">
                <label className="cform-check">
                  <input type="checkbox" checked={st.visible} onChange={(e) => setPlot(i, { visible: e.target.checked })} />
                  {p.label}
                </label>
                <span className="cform-ctl">
                  <ColorInput value={st.color} fallback={p.color} onChange={(c) => setPlot(i, { color: c || p.color })} />
                  {p.kind !== "marks" && (
                    <Sel value={kind} options={PLOT_KINDS} onChange={(k) => setPlot(i, { kind: k === (p.kind ?? "line") ? undefined : k })} ariaLabel={`${p.label} plot type`} />
                  )}
                  {!bar && p.kind !== "marks" && (
                    <>
                      <Sel value={st.width} options={[1, 2, 3, 4].map((w) => ({ v: w, l: `${w}px` }))} onChange={(w) => setPlot(i, { width: w })} ariaLabel="Thickness" />
                      {(kind === "line" || kind === "step" || kind === "area") && <Sel value={st.dash} options={DASH_OPTS} onChange={(d) => setPlot(i, { dash: d })} ariaLabel="Line style" />}
                    </>
                  )}
                </span>
              </div>
            );
          })}
          {(def.fills ?? []).map((f, k) => {
            const st = inst.fills?.[k] ?? { color: f.color, visible: true };
            const put = (patch: Partial<typeof st>) => {
              const fills = (inst.fills ?? defaultFills(def)).map((x, j) => (j === k ? { ...x, ...patch } : x));
              set({ fills });
            };
            return (
              <div key={f.key} className="cform-row">
                <label className="cform-check">
                  <input type="checkbox" checked={st.visible} onChange={(e) => put({ visible: e.target.checked })} />
                  {f.label}
                </label>
                <span className="cform-ctl">
                  <ColorInput value={st.color} fallback={f.color} onChange={(c) => put({ color: c || f.color })} />
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
      {tab === "visibility" &&
        VIS_GROUPS.map((g) => {
          const v = inst.vis?.[g.id] ?? { on: true, min: 1, max: g.max };
          const put = (patch: Partial<typeof v>) => set({ vis: { ...(inst.vis ?? {}), [g.id]: { ...v, ...patch } } });
          return (
            <div key={g.id} className="cform-row">
              <label className="cform-check">
                <input type="checkbox" checked={v.on} onChange={(e) => put({ on: e.target.checked })} />
                {g.label}
              </label>
              <span className="cform-ctl">
                <Num value={v.min} min={1} max={g.max} onChange={(n) => put({ min: n })} width={56} ariaLabel={`${g.label} from`} />
                <span className="hint">–</span>
                <Num value={v.max} min={1} max={g.max} onChange={(n) => put({ max: n })} width={56} ariaLabel={`${g.label} to`} />
              </span>
            </div>
          );
        })}
    </Modal>
  );
}
