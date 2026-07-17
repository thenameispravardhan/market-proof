// Exit Manager — frontend-only control over the entire exit engine.
//
// Every knob the trade manager reads (app/execution/trade_manager.py) is
// an editable field here, grouped by lifecycle stage, with a live worked
// example at the top that recomputes as the operator types. Values persist
// via the same settings mechanism as the Settings page (app_settings table
// → env override → get_settings()), and the engine reads them live, so a
// save applies to the NEXT position opened — open positions keep the exits
// they were armed with.

import { useEffect, useState, type ReactNode } from "react";
import { useGlobalSettings, useUpdateSettings } from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";
import type { GlobalSettings as GS } from "../../types";

type Values = Partial<GS>;

// ---------------------------------------------------------------------------
// Field metadata
// ---------------------------------------------------------------------------

interface NumSpec {
  key: keyof GS;
  label: string;
  hint: string;
  min: number;
  max: number;
  step: number;
}

const CARD_FIELDS: Record<string, NumSpec[]> = {
  entry: [
    {
      key: "ENTRY_MAX_DRIFT_PCT",
      label: "Max entry drift (%)",
      hint: "Anti-chase: if the price already ran more than this past the signal's intended entry, the trade is skipped instead of chased.",
      min: 0.1, max: 10, step: 0.1,
    },
  ],
  stop: [
    {
      key: "DEFAULT_SL_PCT",
      label: "Default stop-loss (%)",
      hint: "Stop distance when no ATR/explicit stop applies. Also defines 1R for targets and trailing.",
      min: 0.5, max: 15, step: 0.5,
    },
    {
      key: "DEFAULT_SL_MIN_PCT",
      label: "Minimum stop distance (%)",
      hint: "Floor: no stop is ever placed tighter than this, however calm the stock.",
      min: 0.1, max: 5, step: 0.1,
    },
    {
      key: "DEFAULT_SL_SMALLCAP_PCT",
      label: "Small-cap stop floor (%)",
      hint: "Wider floor for low-priced stocks, which wiggle more in % terms.",
      min: 0.1, max: 10, step: 0.1,
    },
    {
      key: "ATR_PERIOD",
      label: "ATR period (candles)",
      hint: "Lookback for the volatility measure (Wilder ATR on intraday candles).",
      min: 5, max: 50, step: 1,
    },
    {
      key: "ATR_STOP_MULT",
      label: "ATR multiplier (×)",
      hint: "Stop distance = ATR × this. Bigger = wider stop, fewer whipsaw exits, larger risk per share.",
      min: 0.5, max: 10, step: 0.5,
    },
    {
      key: "ATR_MAX_STOP_PCT",
      label: "ATR stop cap (%)",
      hint: "However volatile the stock, the ATR stop never exceeds this % of entry.",
      min: 1, max: 15, step: 0.5,
    },
  ],
  target: [
    {
      key: "DEFAULT_TARGET_RR",
      label: "Default target (R:R)",
      hint: "Target = entry + this × initial risk. Only used when the signal has no explicit target.",
      min: 0.5, max: 10, step: 0.5,
    },
  ],
  breakeven: [
    {
      key: "BREAKEVEN_AT_PCT",
      label: "Trigger at profit (%)",
      hint: "Once the position is up this much, the stop jumps to the lock level below.",
      min: 0.2, max: 10, step: 0.1,
    },
    {
      key: "BREAKEVEN_LOCK_PCT",
      label: "Lock at (% above entry)",
      hint: "Where the stop lands after the trigger — 0.2% keeps a sliver of profit after costs.",
      min: 0.05, max: 5, step: 0.05,
    },
  ],
  trail: [
    {
      key: "TRAIL_ACTIVATE_R",
      label: "Trail arms at (+R)",
      hint: "The trailing stop switches on once profit reaches this multiple of initial risk.",
      min: 0.5, max: 10, step: 0.25,
    },
    {
      key: "TRAIL_DISTANCE_R",
      label: "Trail distance (R)",
      hint: "Once armed, the stop follows the best price at this distance. Smaller = exits sooner, keeps more of a reversal.",
      min: 0.1, max: 5, step: 0.05,
    },
    {
      key: "SCALE_OUT_R",
      label: "Scale-out at (+R)",
      hint: "Half the position is banked at this profit multiple; the rest rides the trail.",
      min: 0.5, max: 10, step: 0.25,
    },
  ],
  momentum: [
    {
      key: "CONSOLIDATION_WINDOW_SECONDS",
      label: "Consolidation window (s)",
      hint: "How long the price must stay pinned in a range to count as consolidating.",
      min: 30, max: 900, step: 10,
    },
    {
      key: "CONSOLIDATION_RANGE_PCT",
      label: "Consolidation range (%)",
      hint: "“Pinned” means the whole window fits inside this band.",
      min: 0.1, max: 3, step: 0.1,
    },
    {
      key: "CONSOLIDATION_MIN_PROFIT_PCT",
      label: "Consolidation exit: min profit (%)",
      hint: "Only take the consolidation exit when at least this far in profit…",
      min: 0.2, max: 10, step: 0.1,
    },
    {
      key: "CONSOLIDATION_MAX_PROFIT_PCT",
      label: "Consolidation exit: max profit (%)",
      hint: "…and at most this far — beyond it, let the trail manage the exit instead.",
      min: 0.5, max: 20, step: 0.1,
    },
    {
      key: "STALL_WINDOW_SECONDS",
      label: "Stall window (s)",
      hint: "Momentum lookback: how far back the rate-of-change is measured.",
      min: 30, max: 600, step: 10,
    },
    {
      key: "STALL_ROC_PCT",
      label: "Stall threshold (% RoC)",
      hint: "Below this rate-of-change over the window, momentum counts as dead.",
      min: 0.05, max: 3, step: 0.05,
    },
    {
      key: "STALL_MIN_PROFIT_PCT",
      label: "Stall exit: min profit (%)",
      hint: "Only capture a stalled move when at least this far in profit…",
      min: 0.5, max: 15, step: 0.5,
    },
    {
      key: "STALL_MAX_PROFIT_PCT",
      label: "Stall exit: max profit (%)",
      hint: "…and at most this far — beyond it the trailing stop takes over.",
      min: 1, max: 25, step: 0.5,
    },
  ],
  time: [
    {
      key: "MAX_HOLD_SECONDS",
      label: "Max hold (seconds)",
      hint: "Hard time limit per position — force-closed at the last quote (TIME_EXIT) when it elapses. 1080 = 18 minutes.",
      min: 60, max: 21600, step: 60,
    },
  ],
};

// ---------------------------------------------------------------------------
// Reusable rows + card shell
// ---------------------------------------------------------------------------

interface RowProps {
  spec: NumSpec;
  values: Values;
  onChange: (key: keyof GS, value: number) => void;
}

function NumRow({ spec, values, onChange }: RowProps) {
  const { key, label, hint, min, max, step } = spec;
  const raw = values[key];
  return (
    <div className="field">
      <label htmlFor={`exit-${key}`}>{label}</label>
      <input
        id={`exit-${key}`}
        type="number"
        min={min}
        max={max}
        step={step}
        value={typeof raw === "number" ? raw : ""}
        onChange={(e) => onChange(key, Number(e.target.value))}
      />
      <span className="field-hint">{hint}</span>
    </div>
  );
}

interface ToggleRowProps {
  k: keyof GS;
  label: string;
  hintOn: string;
  hintOff: string;
  values: Values;
  onChange: (key: keyof GS, value: boolean) => void;
}

function ToggleRow({ k, label, hintOn, hintOff, values, onChange }: ToggleRowProps) {
  const on = Boolean(values[k] ?? true);
  return (
    <div className="field">
      <label>{label}</label>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <Toggle on={on} data-testid={`exit-toggle-${k}`} onChange={(next) => onChange(k, next)} />
        <span className="field-hint" style={{ marginTop: 0 }}>{on ? hintOn : hintOff}</span>
      </div>
    </div>
  );
}

interface CardProps {
  title: string;
  keys: (keyof GS)[];
  values: Values;
  note?: string;
  children: ReactNode;
}

function Card({ title, keys, values, note, children }: CardProps) {
  const update = useUpdateSettings();
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset the ✓ when THIS card's values actually change. Keyed on content,
  // not object identity: the post-save refetch rebuilds `values` with the
  // same numbers, and that must not wipe a fresh confirmation.
  const slice = JSON.stringify(keys.map((k) => values[k]));
  useEffect(() => {
    setSaved(false);
  }, [slice]);

  const handleSave = async () => {
    setError(null);
    const slice: Record<string, unknown> = {};
    for (const k of keys) {
      if (values[k] !== undefined) slice[k] = values[k];
    }
    try {
      await update.mutateAsync({ global: slice as Partial<GS> as GS });
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="widget" style={{ marginTop: 16 }}>
      <h3>{title}</h3>
      {children}
      {note && <p className="empty">{note}</p>}
      {error && <p className="pnl-neg">{error}</p>}
      {saved && <p className="pnl-pos">✓ Saved — applies to positions opened from now on</p>}
      <button className="primary" onClick={handleSave} disabled={update.isPending}>
        {update.isPending ? "Saving…" : "Save"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Lifecycle strip — the worked example
// ---------------------------------------------------------------------------

const rupees = (v: number) => `₹${v.toFixed(2)}`;

function LifecycleStrip({ v }: { v: Values }) {
  const entry = 100;
  const slPct = Number(v.DEFAULT_SL_PCT ?? 6);
  const risk = (entry * slPct) / 100; // 1R in ₹
  const stop = entry - risk;
  const atrOn = Boolean(v.ATR_ENABLED ?? true);
  const atrCapStop = entry * (1 - Number(v.ATR_MAX_STOP_PCT ?? 8) / 100);
  const beOn = Boolean(v.BREAKEVEN_ENABLED ?? true);
  const beAt = entry * (1 + Number(v.BREAKEVEN_AT_PCT ?? 2) / 100);
  const beLock = entry * (1 + Number(v.BREAKEVEN_LOCK_PCT ?? 0.2) / 100);
  const trailAt = entry + Number(v.TRAIL_ACTIVATE_R ?? 1.5) * risk;
  const trailDist = Number(v.TRAIL_DISTANCE_R ?? 0.5) * risk;
  const scaleOn = Boolean(v.SCALE_OUT_ENABLED ?? true);
  const scaleAt = entry + Number(v.SCALE_OUT_R ?? 2) * risk;
  const target = entry + Number(v.DEFAULT_TARGET_RR ?? 3) * risk;
  const holdMin = Math.round(Number(v.MAX_HOLD_SECONDS ?? 1080) / 60);
  const squareOff = String(v.SQUARE_OFF_TIME_IST ?? "15:10");

  const steps: string[] = [];
  steps.push(
    `BUY ₹${entry} → initial stop ${rupees(stop)} (risk 1R = ${rupees(risk)})` +
      (atrOn ? ` · ATR may widen it, never past ${rupees(atrCapStop)}` : " · ATR stops off"),
  );
  if (beOn) steps.push(`price ${rupees(beAt)} → stop locks to ${rupees(beLock)} (can no longer lose)`);
  steps.push(`price ${rupees(trailAt)} → trailing stop armed, follows the peak by ${rupees(trailDist)}`);
  if (scaleOn) steps.push(`price ${rupees(scaleAt)} → half the position banked, the rest rides the trail`);
  steps.push(`price ${rupees(target)} → default target (explicit rule/AI target always exits IN FULL)`);
  steps.push(`${holdMin} min elapsed → TIME_EXIT · ${squareOff} IST → EOD square-off, always`);

  return (
    <div className="widget" data-testid="exit-lifecycle">
      <h3>Position Lifecycle — live example (updates as you edit)</h3>
      <ol style={{ margin: "4px 0 4px 20px", padding: 0 }}>
        {steps.map((s) => (
          <li key={s} className="mono" style={{ marginBottom: 4 }}>{s}</li>
        ))}
      </ol>
      <p className="empty">
        Example assumes a BUY at ₹100 with the % stop. SELL trades mirror it. On a
        real trade the stop may come from ATR or the signal itself — same rules, real numbers.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function ExitManager() {
  const { data: settings, isLoading } = useGlobalSettings();
  const [values, setValues] = useState<Values>({});

  useEffect(() => {
    if (settings?.global) setValues({ ...settings.global });
  }, [settings]);

  const setNum = (key: keyof GS, value: number) =>
    setValues((prev) => ({ ...prev, [key]: value }));
  const setBool = (key: keyof GS, value: boolean) =>
    setValues((prev) => ({ ...prev, [key]: value }));

  if (isLoading) return <p className="empty">Loading…</p>;

  const F = CARD_FIELDS;
  return (
    <div>
      <LifecycleStrip v={values} />

      <Card title="1 · Entry Quality" keys={["ENTRY_MAX_DRIFT_PCT"]} values={values}>
        {F.entry.map((s) => <NumRow key={s.key} spec={s} values={values} onChange={setNum} />)}
      </Card>

      <Card
        title="2 · Initial Stop-Loss"
        keys={["DEFAULT_SL_PCT", "DEFAULT_SL_MIN_PCT", "DEFAULT_SL_SMALLCAP_PCT", "ATR_ENABLED", "ATR_PERIOD", "ATR_STOP_MULT", "ATR_MAX_STOP_PCT"]}
        values={values}
      >
        <ToggleRow
          k="ATR_ENABLED"
          label="Volatility-aware stops (ATR)"
          hintOn="Stop distance adapts to each stock's volatility: ATR × multiplier, clamped between the minimum stop and the cap. Calm stock = tight stop, volatile stock = wide stop."
          hintOff="Fixed % stops only — every stock gets the same stop distance regardless of how it moves."
          values={values}
          onChange={setBool}
        />
        {F.stop.map((s) => <NumRow key={s.key} spec={s} values={values} onChange={setNum} />)}
      </Card>

      <Card
        title="3 · Target"
        keys={["DEFAULT_TARGET_RR"]}
        values={values}
        note="Hard rule (not configurable): when the AI or a rule supplies an explicit target, reaching it exits the FULL position. Scale-out and trailing apply only to open-ended trades."
      >
        {F.target.map((s) => <NumRow key={s.key} spec={s} values={values} onChange={setNum} />)}
      </Card>

      <Card
        title="4 · Breakeven Lock"
        keys={["BREAKEVEN_ENABLED", "BREAKEVEN_AT_PCT", "BREAKEVEN_LOCK_PCT"]}
        values={values}
      >
        <ToggleRow
          k="BREAKEVEN_ENABLED"
          label="Breakeven lock"
          hintOn="Once profit reaches the trigger, the stop jumps above entry — the trade can no longer turn into a loss."
          hintOff="The stop stays at its initial level until the trail arms — a winner can still round-trip to a full loss."
          values={values}
          onChange={setBool}
        />
        {F.breakeven.map((s) => <NumRow key={s.key} spec={s} values={values} onChange={setNum} />)}
      </Card>

      <Card
        title="5 · Trailing Stop & Scale-Out"
        keys={["SCALE_OUT_ENABLED", "SCALE_OUT_R", "TRAIL_ACTIVATE_R", "TRAIL_DISTANCE_R"]}
        values={values}
      >
        <ToggleRow
          k="SCALE_OUT_ENABLED"
          label="Scale-out"
          hintOn="Bank half at the scale-out level, trail the remainder — locks in profit while keeping upside."
          hintOff="No partial profit-taking — the whole position rides until an exit fires."
          values={values}
          onChange={setBool}
        />
        {F.trail.map((s) => <NumRow key={s.key} spec={s} values={values} onChange={setNum} />)}
      </Card>

      <Card
        title="6 · Momentum-Death Exits"
        keys={["CONSOLIDATION_EXIT_ENABLED", "CONSOLIDATION_WINDOW_SECONDS", "CONSOLIDATION_RANGE_PCT", "CONSOLIDATION_MIN_PROFIT_PCT", "CONSOLIDATION_MAX_PROFIT_PCT", "STALL_EXIT_ENABLED", "STALL_WINDOW_SECONDS", "STALL_ROC_PCT", "STALL_MIN_PROFIT_PCT", "STALL_MAX_PROFIT_PCT"]}
        values={values}
        note="News moves die fast. These two exits capture a profit that has stopped growing instead of watching it revert. Each min/max pair is the profit band where the exit is allowed to fire."
      >
        <ToggleRow
          k="CONSOLIDATION_EXIT_ENABLED"
          label="Consolidation exit"
          hintOn="If the price goes flat (pinned in a narrow range) while modestly in profit, take the profit."
          hintOff="Flat price action is ignored — only stops/targets/trail/time can exit."
          values={values}
          onChange={setBool}
        />
        <ToggleRow
          k="STALL_EXIT_ENABLED"
          label="Stall exit"
          hintOn="If momentum dies (rate-of-change below threshold) while well in profit, capture before the reversion."
          hintOff="Dead momentum is ignored — only stops/targets/trail/time can exit."
          values={values}
          onChange={setBool}
        />
        {F.momentum.map((s) => <NumRow key={s.key} spec={s} values={values} onChange={setNum} />)}
      </Card>

      <Card
        title="7 · Time Limits"
        keys={["MAX_HOLD_SECONDS", "SQUARE_OFF_TIME_IST"]}
        values={values}
        note="The EOD square-off cannot be disabled — intraday only, no carry-forward is a design invariant. You may move it earlier (09:30–15:15), never later."
      >
        {F.time.map((s) => <NumRow key={s.key} spec={s} values={values} onChange={setNum} />)}
        <div className="field">
          <label htmlFor="exit-SQUARE_OFF_TIME_IST">EOD square-off (IST)</label>
          <input
            id="exit-SQUARE_OFF_TIME_IST"
            type="time"
            min="09:30"
            max="15:15"
            value={String(values.SQUARE_OFF_TIME_IST ?? "15:10")}
            onChange={(e) =>
              setValues((prev) => ({ ...prev, SQUARE_OFF_TIME_IST: e.target.value }))
            }
          />
          <span className="field-hint">
            Every open position is force-flattened at this time, before the 15:30 close
            and the broker&apos;s own MIS auto-square-off.
          </span>
        </div>
      </Card>
    </div>
  );
}
