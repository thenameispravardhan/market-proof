// UserSettings — Tools → User Settings: the terminal's preferences in one
// place (Fyers' global settings dialog): General, Order window, Option
// chain, Scalper and Charts. Some live elsewhere already (theme, privacy,
// the trading flags, chart settings, layout autosave); this edits them in
// place and keeps the new ones (default quantity / order type, chain
// strikes, scalper lots) under trade:userSettings.

import { useState } from "react";
import { getTheme, setTheme } from "../../lib/theme";
import { Check, Modal, Num, Row, Section, Sel } from "./chartUi";
import { loadSettings, saveSettings, type ChartSettings } from "./chartSettings";

export interface UserPrefs {
  defaultQty: number;
  defaultOrderType: "MARKET" | "LIMIT";
  chainStrikes: number;
  chainOiBars: boolean;
  scalperLots: number;
}

export const USER_PREFS_KEY = "trade:userSettings";
export const USER_PREFS_DEFAULT: UserPrefs = { defaultQty: 1, defaultOrderType: "MARKET", chainStrikes: 12, chainOiBars: true, scalperLots: 1 };
/** Fired after chart:settings is edited outside a chart (charts reload theirs). */
export const CHART_SETTINGS_EVENT = "chart:settings-ext";

export function loadUserPrefs(): UserPrefs {
  try {
    return { ...USER_PREFS_DEFAULT, ...(JSON.parse(localStorage.getItem(USER_PREFS_KEY) ?? "{}") as Partial<UserPrefs>) };
  } catch {
    return { ...USER_PREFS_DEFAULT };
  }
}

export interface UserSettingsHost {
  tset: { instant: boolean; showPos: boolean; showOrders: boolean; plus: boolean };
  onTset: (k: "instant" | "showPos" | "showOrders" | "plus", v: boolean) => void;
  privacy: boolean;
  onPrivacy: (v: boolean) => void;
  autosave: boolean;
  onAutosave: (v: boolean) => void;
  onPrefs: (p: UserPrefs) => void;
}

export function UserSettingsDialog({ host, onClose }: { host: UserSettingsHost; onClose: () => void }) {
  const [tab, setTab] = useState("general");
  const [prefs, setPrefs] = useState(loadUserPrefs);
  const [cs, setCs] = useState<ChartSettings>(loadSettings);
  const [skin, setSkin] = useState(getTheme);
  const putPrefs = (patch: Partial<UserPrefs>) => {
    const next = { ...prefs, ...patch };
    setPrefs(next);
    try {
      localStorage.setItem(USER_PREFS_KEY, JSON.stringify(next));
      if (patch.scalperLots !== undefined) localStorage.setItem("scalp:lots", JSON.stringify(next.scalperLots));
    } catch {
      /* best-effort */
    }
    host.onPrefs(next);
  };
  const putChart = <K extends keyof ChartSettings>(k: K, v: ChartSettings[K]) => {
    const next = { ...loadSettings(), [k]: v };
    setCs(next);
    saveSettings(next);
    window.dispatchEvent(new Event(CHART_SETTINGS_EVENT));
  };
  const tabs = [
    { id: "general", label: "General" },
    { id: "order", label: "Order window" },
    { id: "chain", label: "Option chain" },
    { id: "scalper", label: "Scalper" },
    { id: "charts", label: "Charts" },
  ];
  return (
    <Modal title="User settings" onClose={onClose} width={560} tabs={tabs} tab={tab} onTab={setTab} testid="user-settings"
      footer={<><span className="grow" /><button type="button" className="cbtn primary" onClick={onClose} data-testid="user-settings-ok">Done</button></>}>
      {tab === "general" && (
        <>
          <Section title="Appearance">
            <Row label="Theme">
              <Sel value={skin} options={[{ v: "dark", l: "Dark" }, { v: "light", l: "Light" }]} onChange={(v) => { setTheme(v); setSkin(v); }} ariaLabel="Theme" />
            </Row>
            <Check label="Privacy — mask P&L and funds" checked={host.privacy} onChange={host.onPrivacy} />
          </Section>
          <Section title="Notifications">
            <Check label="Sound for executions and alerts" checked={cs.sound} onChange={(v) => putChart("sound", v)} />
            <Row label="Order notifications">
              <Sel value={cs.notifications} options={[{ v: "all", l: "All events" }, { v: "rejections", l: "Only rejections" }, { v: "off", l: "Off" }]} onChange={(v) => putChart("notifications", v)} ariaLabel="Order notifications" />
            </Row>
          </Section>
        </>
      )}
      {tab === "order" && (
        <Section title="Order window">
          <Row label="Default quantity"><Num value={prefs.defaultQty} min={1} onChange={(v) => putPrefs({ defaultQty: Math.max(1, Math.round(v)) })} ariaLabel="Default quantity" /></Row>
          <Row label="Default order type">
            <Sel value={prefs.defaultOrderType} options={[{ v: "MARKET", l: "Market" }, { v: "LIMIT", l: "Limit" }]} onChange={(v) => putPrefs({ defaultOrderType: v })} ariaLabel="Default order type" />
          </Row>
          <Check label="Confirm before placing chart / scalper orders" checked={!host.tset.instant} onChange={(v) => host.onTset("instant", !v)} />
          <Check label="Positions on the chart (P&L, SL / TP)" checked={host.tset.showPos} onChange={(v) => host.onTset("showPos", v)} />
          <Check label="Pending orders on the chart" checked={host.tset.showOrders} onChange={(v) => host.onTset("showOrders", v)} />
          <Check label="'+' button on the price scale" checked={host.tset.plus} onChange={(v) => host.onTset("plus", v)} />
        </Section>
      )}
      {tab === "chain" && (
        <Section title="Option chain">
          <Row label="Strikes around ATM"><Num value={prefs.chainStrikes} min={4} max={40} onChange={(v) => putPrefs({ chainStrikes: Math.max(4, Math.min(40, Math.round(v))) })} ariaLabel="Strikes" /></Row>
          <Check label="Open-interest bars in the chain" checked={prefs.chainOiBars} onChange={(v) => putPrefs({ chainOiBars: v })} />
        </Section>
      )}
      {tab === "scalper" && (
        <Section title="Option scalper">
          <Row label="Default lots"><Num value={prefs.scalperLots} min={1} max={500} onChange={(v) => putPrefs({ scalperLots: Math.max(1, Math.round(v)) })} ariaLabel="Default lots" /></Row>
          <Check label="Instant orders (hotkeys armed, no confirm)" checked={host.tset.instant} onChange={(v) => host.onTset("instant", v)} />
        </Section>
      )}
      {tab === "charts" && (
        <Section title="Charts">
          <Check label="Autosave the chart layout" checked={host.autosave} onChange={host.onAutosave} />
          <Check label="Corporate actions on bars (dividends, splits, results)" checked={cs.showEvents} onChange={(v) => putChart("showEvents", v)} />
          <Check label="Executions on the chart" checked={cs.executions} onChange={(v) => putChart("executions", v)} />
          <Check label="Buy / sell buttons on the chart" checked={cs.buySellButtons} onChange={(v) => putChart("buySellButtons", v)} />
          <Check label="Countdown to bar close" checked={cs.countdown} onChange={(v) => putChart("countdown", v)} />
        </Section>
      )}
    </Modal>
  );
}
