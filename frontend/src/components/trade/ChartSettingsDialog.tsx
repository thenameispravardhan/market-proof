// ChartSettingsDialog — TradingView's chart settings: Symbol, Status line,
// Scales and lines, Canvas, Trading, Events, Alerts. Edits apply live;
// Cancel restores what was there. The footer's Template menu saves / applies
// named settings sets and resets to defaults.

import { useState } from "react";
import { DATE_FORMATS, TIMEZONES } from "./chartData";
import { Check, ColorInput, Modal, Num, Row, Section, Sel } from "./chartUi";
import { DEFAULT_SETTINGS, SETTINGS_TEMPLATES_KEY, type ChartSettings, type LineVisibility } from "./chartSettings";

export interface TradingFlags {
  instant: boolean;
  showPos: boolean;
  showOrders: boolean;
  plus: boolean;
}

const VIS_OPTS: { v: LineVisibility; l: string }[] = [
  { v: "hover", l: "Visible on mouse over" },
  { v: "always", l: "Always visible" },
  { v: "never", l: "Always invisible" },
];

function loadTemplates(): Record<string, ChartSettings> {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_TEMPLATES_KEY) ?? "{}") as Record<string, ChartSettings>;
  } catch {
    return {};
  }
}

export default function ChartSettingsDialog({
  value,
  onChange,
  onClose,
  theme,
  trading,
  onTrading,
  initialTab = "symbol",
}: {
  value: ChartSettings;
  onChange: (s: ChartSettings) => void;
  onClose: () => void;
  theme: { up: string; down: string; text: string; grid: string; bg: string; accent: string; crosshair: string; border: string };
  trading?: TradingFlags;
  onTrading?: (k: keyof TradingFlags, v: boolean) => void;
  initialTab?: string;
}) {
  const [orig] = useState(value);
  const [tab, setTab] = useState(initialTab);
  const [tplOpen, setTplOpen] = useState(false);
  const [templates, setTemplates] = useState(loadTemplates);
  const s = value;
  const set = <K extends keyof ChartSettings>(k: K, v: ChartSettings[K]) => onChange({ ...s, [k]: v });
  const tabs = [
    { id: "symbol", label: "Symbol" },
    { id: "status", label: "Status line" },
    { id: "scales", label: "Scales and lines" },
    { id: "canvas", label: "Canvas" },
    { id: "trading", label: "Trading" },
    { id: "events", label: "Events" },
    { id: "alerts", label: "Alerts" },
  ];
  const pair = (onK: keyof ChartSettings, upK: keyof ChartSettings, downK: keyof ChartSettings, label: string, fbUp: string, fbDown: string) => (
    <Check label={label} checked={s[onK] as boolean} onChange={(v) => set(onK, v as never)}>
      <ColorInput value={s[upK] as string} fallback={fbUp} onChange={(c) => set(upK, c as never)} title={`${label} up`} />
      <ColorInput value={s[downK] as string} fallback={fbDown} onChange={(c) => set(downK, c as never)} title={`${label} down`} />
    </Check>
  );
  const saveTemplate = () => {
    const name = window.prompt("Template name");
    if (!name) return;
    const next = { ...templates, [name]: s };
    setTemplates(next);
    try { localStorage.setItem(SETTINGS_TEMPLATES_KEY, JSON.stringify(next)); } catch { /* best-effort */ }
    setTplOpen(false);
  };
  const delTemplate = (name: string) => {
    const next = { ...templates };
    delete next[name];
    setTemplates(next);
    try { localStorage.setItem(SETTINGS_TEMPLATES_KEY, JSON.stringify(next)); } catch { /* best-effort */ }
  };
  return (
    <Modal
      title="Settings"
      onClose={() => { onChange(orig); onClose(); }}
      width={600}
      tabs={tabs}
      tab={tab}
      onTab={setTab}
      testid="chart-settings"
      footer={
        <>
          <div className="chart-menu-wrap">
            <button type="button" className="cbtn" onClick={() => setTplOpen((o) => !o)}>Template ▾</button>
            {tplOpen && (
              <div className="chart-menu cdrop up">
                <button type="button" className="chart-menu-item" onClick={saveTemplate}>Save as…</button>
                <button type="button" className="chart-menu-item" onClick={() => { onChange({ ...DEFAULT_SETTINGS }); setTplOpen(false); }}>Apply defaults</button>
                {Object.keys(templates).length > 0 && <div className="chart-menu-sep" />}
                {Object.entries(templates).map(([name, t]) => (
                  <div key={name} className="chart-menu-row">
                    <button type="button" className="chart-menu-item" onClick={() => { onChange({ ...DEFAULT_SETTINGS, ...t }); setTplOpen(false); }}>{name}</button>
                    <button type="button" className="chart-menu-x" onClick={() => delTemplate(name)} title="Delete template">✕</button>
                  </div>
                ))}
              </div>
            )}
          </div>
          <span className="grow" />
          <button type="button" className="cbtn" onClick={() => { onChange(orig); onClose(); }}>Cancel</button>
          <button type="button" className="cbtn primary" onClick={onClose} data-testid="chart-settings-ok">Ok</button>
        </>
      }
    >
      {tab === "symbol" && (
        <>
          <Section title="Candles">
            <Check label="Color bars based on previous close" checked={s.colorPrevClose} onChange={(v) => set("colorPrevClose", v)} />
            {pair("bodyOn", "upColor", "downColor", "Body", theme.up, theme.down)}
            {pair("borderOn", "borderUp", "borderDown", "Borders", s.upColor || theme.up, s.downColor || theme.down)}
            {pair("wickOn", "wickUp", "wickDown", "Wick", s.upColor || theme.up, s.downColor || theme.down)}
          </Section>
          <Section title="Data modification">
            <Row label="Precision">
              <Sel value={s.precision ?? -1} options={[{ v: -1, l: "Default" }, ...[0, 1, 2, 3, 4, 5].map((n) => ({ v: n, l: n === 0 ? "1" : `1/${10 ** n}` }))]} onChange={(v) => set("precision", v < 0 ? null : v)} ariaLabel="Precision" />
            </Row>
            <Row label="Timezone">
              <Sel value={s.timezone} options={TIMEZONES.map((t) => ({ v: t.id, l: t.label }))} onChange={(v) => set("timezone", v)} ariaLabel="Timezone" />
            </Row>
          </Section>
          <Section title="Renko / Kagi / P&F / Range">
            <Row label="Box size" hint="0 = automatic (ATR 14)">
              <Num value={s.boxSize} min={0} step={0.05} onChange={(v) => set("boxSize", v)} ariaLabel="Box size" />
              <span className="hint">{s.boxSize ? "" : "auto (ATR)"}</span>
            </Row>
            <Row label="P&F reversal (boxes)"><Num value={s.reversal} min={1} max={10} onChange={(v) => set("reversal", Math.round(v))} ariaLabel="Reversal" /></Row>
            <Row label="Line break: lines"><Num value={s.lineBreak} min={1} max={10} onChange={(v) => set("lineBreak", Math.round(v))} ariaLabel="Line break" /></Row>
          </Section>
        </>
      )}
      {tab === "status" && (
        <>
          <Section title="Symbol">
            <Check label="Title" checked={s.showTitle} onChange={(v) => set("showTitle", v)}>
              <Sel value={s.titleMode} options={[{ v: "description", l: "Description" }, { v: "ticker", l: "Ticker" }, { v: "both", l: "Ticker and description" }]} onChange={(v) => set("titleMode", v)} ariaLabel="Title" />
            </Check>
            <Check label="Chart values (OHLC)" checked={s.showOhlc} onChange={(v) => set("showOhlc", v)} />
            <Check label="Bar change values" checked={s.showBarChange} onChange={(v) => set("showBarChange", v)} />
            <Check label="Volume" checked={s.showVolume} onChange={(v) => set("showVolume", v)} />
            <Check label="Last day change values" checked={s.showLastDayChange} onChange={(v) => set("showLastDayChange", v)} />
          </Section>
          <Section title="Indicators">
            <Check label="Titles" checked={s.indTitles} onChange={(v) => set("indTitles", v)} />
            <Check label="Arguments" checked={s.indArgs} onChange={(v) => set("indArgs", v)} />
            <Check label="Values" checked={s.indValues} onChange={(v) => set("indValues", v)} />
            <Check label="Background" checked={s.legendBg} onChange={(v) => set("legendBg", v)}>
              <input type="range" min={0} max={100} value={Math.round(s.legendBgOpacity * 100)} onChange={(e) => set("legendBgOpacity", Number(e.target.value) / 100)} aria-label="Background opacity" />
            </Check>
          </Section>
        </>
      )}
      {tab === "scales" && (
        <>
          <Section title="Price scale">
            <Row label="Scale modes (A and L)"><Sel value={s.scaleModesButtons} options={VIS_OPTS} onChange={(v) => set("scaleModesButtons", v)} ariaLabel="Scale modes" /></Row>
            <Row label="Scales placement"><Sel value={s.scaleSide} options={[{ v: "right", l: "Right" }, { v: "left", l: "Left" }]} onChange={(v) => set("scaleSide", v)} ariaLabel="Scales placement" /></Row>
          </Section>
          <Section title="Price labels & lines">
            <Check label="Countdown to bar close" checked={s.countdown} onChange={(v) => set("countdown", v)} />
            <Row label="Symbol">
              <Sel
                value={s.lastPriceLabel && s.lastPriceLine ? "vl" : s.lastPriceLabel ? "v" : s.lastPriceLine ? "l" : "h"}
                options={[{ v: "vl", l: "Value, line" }, { v: "v", l: "Value" }, { v: "l", l: "Line" }, { v: "h", l: "Hidden" }]}
                onChange={(v) => onChange({ ...s, lastPriceLabel: v === "vl" || v === "v", lastPriceLine: v === "vl" || v === "l" })}
                ariaLabel="Symbol label"
              />
            </Row>
            <Check label="Symbol name label" checked={s.symbolNameLabel} onChange={(v) => set("symbolNameLabel", v)} />
            {([
              ["Previous day close", "prevCloseLabel", "prevCloseLine"],
              ["High and low", "highLowLabels", "highLowLines"],
              ["Average close", "avgCloseLabel", "avgCloseLine"],
              ["Bid and ask", "bidAskLabels", "bidAskLines"],
            ] as const).map(([label, lk, nk]) => (
              <Row key={lk} label={label}>
                <Sel
                  value={s[lk] && s[nk] ? "vl" : s[lk] ? "v" : s[nk] ? "l" : "h"}
                  options={[{ v: "vl", l: "Value, line" }, { v: "v", l: "Value" }, { v: "l", l: "Line" }, { v: "h", l: "Hidden" }]}
                  onChange={(v) => onChange({ ...s, [lk]: v === "vl" || v === "v", [nk]: v === "vl" || v === "l" })}
                  ariaLabel={label}
                />
              </Row>
            ))}
            <Row label="Indicators and financials">
              <Sel
                value={s.indNameLabels && s.indValueLabels ? "nv" : s.indNameLabels ? "n" : s.indValueLabels ? "v" : "h"}
                options={[{ v: "v", l: "Value" }, { v: "n", l: "Name" }, { v: "nv", l: "Name, value" }, { v: "h", l: "Hidden" }]}
                onChange={(v) => onChange({ ...s, indNameLabels: v === "n" || v === "nv", indValueLabels: v === "v" || v === "nv" })}
                ariaLabel="Indicator labels"
              />
            </Row>
          </Section>
          <Section title="Time scale">
            <Row label="Date format"><Sel value={s.dateFormat} options={DATE_FORMATS.map((f) => ({ v: f, l: f }))} onChange={(v) => set("dateFormat", v)} ariaLabel="Date format" /></Row>
            <Row label="Time hours format"><Sel value={s.hour12 ? "12" : "24"} options={[{ v: "24", l: "24-hours" }, { v: "12", l: "12-hours" }]} onChange={(v) => set("hour12", v === "12")} ariaLabel="Hours format" /></Row>
          </Section>
        </>
      )}
      {tab === "canvas" && (
        <>
          <Section title="Chart basic styles">
            <Row label="Theme"><Sel value={s.theme} options={[{ v: "app", l: "App theme" }, { v: "dark", l: "Dark" }, { v: "light", l: "Light" }]} onChange={(v) => set("theme", v)} ariaLabel="Theme" /></Row>
            <Row label="Background">
              <Sel value={s.bgType} options={[{ v: "solid", l: "Solid" }, { v: "gradient", l: "Gradient" }]} onChange={(v) => set("bgType", v)} ariaLabel="Background type" />
              <ColorInput value={s.bg1} fallback={theme.bg} onChange={(c) => set("bg1", c)} />
              {s.bgType === "gradient" && <ColorInput value={s.bg2} fallback={theme.bg} onChange={(c) => set("bg2", c)} />}
            </Row>
            <Row label="Grid lines">
              <Sel value={s.grid} options={[{ v: "both", l: "Vert and horz" }, { v: "vert", l: "Vert only" }, { v: "horz", l: "Horz only" }, { v: "none", l: "None" }]} onChange={(v) => set("grid", v)} ariaLabel="Grid lines" />
              <ColorInput value={s.gridColor} fallback={theme.grid} onChange={(c) => set("gridColor", c)} />
            </Row>
            <Row label="Crosshair">
              <ColorInput value={s.crosshairColor} fallback={theme.crosshair} onChange={(c) => set("crosshairColor", c)} />
              <Sel value={s.crosshairWidth} options={[1, 2, 3, 4].map((w) => ({ v: w, l: `${w}px` }))} onChange={(v) => set("crosshairWidth", v)} ariaLabel="Crosshair width" />
              <Sel value={s.crosshairStyle} options={[{ v: 0 as const, l: "Solid" }, { v: 1 as const, l: "Dotted" }, { v: 2 as const, l: "Dashed" }, { v: 3 as const, l: "Large dashed" }]} onChange={(v) => set("crosshairStyle", v)} ariaLabel="Crosshair style" />
            </Row>
            <Check label="Watermark" checked={s.watermark} onChange={(v) => set("watermark", v)}>
              <ColorInput value={s.watermarkColor} fallback={theme.text} onChange={(c) => set("watermarkColor", c)} />
            </Check>
          </Section>
          <Section title="Scales">
            <Row label="Text">
              <ColorInput value={s.textColor} fallback={theme.text} onChange={(c) => set("textColor", c)} />
              <Sel value={s.fontSize} options={[10, 11, 12, 13, 14, 16].map((n) => ({ v: n, l: String(n) }))} onChange={(v) => set("fontSize", v)} ariaLabel="Font size" />
            </Row>
            <Row label="Lines"><ColorInput value={s.scaleLineColor} fallback={theme.border} onChange={(c) => set("scaleLineColor", c)} /></Row>
          </Section>
          <Section title="Buttons">
            <Row label="Navigation"><Sel value={s.navButtons} options={VIS_OPTS} onChange={(v) => set("navButtons", v)} ariaLabel="Navigation buttons" /></Row>
            <Row label="Pane"><Sel value={s.paneButtons} options={VIS_OPTS} onChange={(v) => set("paneButtons", v)} ariaLabel="Pane buttons" /></Row>
          </Section>
          <Section title="Margins">
            <Row label="Top (%)"><Num value={s.marginTop} min={0} max={40} onChange={(v) => set("marginTop", v)} ariaLabel="Top margin" /></Row>
            <Row label="Bottom (%)"><Num value={s.marginBottom} min={0} max={40} onChange={(v) => set("marginBottom", v)} ariaLabel="Bottom margin" /></Row>
            <Row label="Right (bars)"><Num value={s.marginRight} min={0} max={200} onChange={(v) => set("marginRight", v)} ariaLabel="Right margin" /></Row>
          </Section>
        </>
      )}
      {tab === "trading" && (
        <>
          <Section title="General">
            <Check label="Buy/sell buttons" checked={s.buySellButtons} onChange={(v) => set("buySellButtons", v)} />
            {trading && onTrading && <Check label="Instant orders placement (no confirm)" checked={trading.instant} onChange={(v) => onTrading("instant", v)} />}
            <Check label="Play sound for executions and alerts" checked={s.sound} onChange={(v) => set("sound", v)} />
            <Row label="Notifications"><Sel value={s.notifications} options={[{ v: "all", l: "All events" }, { v: "rejections", l: "Only rejections" }, { v: "off", l: "Off" }]} onChange={(v) => set("notifications", v)} ariaLabel="Notifications" /></Row>
          </Section>
          <Section title="Appearance">
            {trading && onTrading && <Check label="Positions" checked={trading.showPos} onChange={(v) => onTrading("showPos", v)} />}
            <Row label="Profit & loss"><Sel value={s.plMode} options={[{ v: "money", l: "Money" }, { v: "percent", l: "Percentage" }]} onChange={(v) => set("plMode", v)} ariaLabel="Profit and loss" /></Row>
            <Check label="Reverse button on hover" checked={s.reverseButton} onChange={(v) => set("reverseButton", v)} />
            {trading && onTrading && <Check label="Orders" checked={trading.showOrders} onChange={(v) => onTrading("showOrders", v)} />}
            {trading && onTrading && <Check label="'+' button on the price scale" checked={trading.plus} onChange={(v) => onTrading("plus", v)} />}
            <Row label="Orders & positions alignment"><Sel value={s.ordersAlign} options={[{ v: "right", l: "Right" }, { v: "left", l: "Left" }]} onChange={(v) => set("ordersAlign", v)} ariaLabel="Alignment" /></Row>
          </Section>
        </>
      )}
      {tab === "events" && (
        <Section title="Events">
          <Check label="Dividends, splits, earnings & bonus (from filed announcements)" checked={s.showEvents} onChange={(v) => set("showEvents", v)} />
          <Check label="Session breaks" checked={s.sessionBreaks} onChange={(v) => set("sessionBreaks", v)} />
          <Check label="Marks on bars (strategy trades, events)" checked={s.showMarks} onChange={(v) => set("showMarks", v)} />
        </Section>
      )}
      {tab === "alerts" && (
        <Section title="Alerts">
          <Check label="Alert lines" checked={s.alertLines} onChange={(v) => set("alertLines", v)}>
            <ColorInput value={s.alertColor} fallback={theme.accent} onChange={(c) => set("alertColor", c)} />
          </Check>
        </Section>
      )}
    </Modal>
  );
}
