// GlobalSettings — editable global risk settings form.
import { useEffect, useState } from "react";
import { useGlobalSettings, useUpdateSettings } from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";
import type { GlobalSettings as GS } from "../../types";

type NumField = Exclude<
  keyof GS,
  "TRADING_MODE" | "PRE_LLM_FILTER_ENABLED" | "AI_ANALYSIS_ENABLED"
>;

const FIELDS: { key: NumField; label: string; min: number; max: number; step: number }[] = [
  { key: "MAX_CAPITAL_RISK_PCT", label: "Max capital risk (%)", min: 0.1, max: 100, step: 0.1 },
  { key: "DAILY_MAX_LOSS_PCT", label: "Daily max loss (%)", min: 0.1, max: 100, step: 0.1 },
  { key: "MAX_CONCURRENT_POSITIONS", label: "Max concurrent positions", min: 1, max: 100, step: 1 },
  { key: "MAX_SINGLE_POSITION_PCT", label: "Max single position (%)", min: 0.1, max: 100, step: 0.1 },
  { key: "MIN_LIQUIDITY_CRORE", label: "Min liquidity (₹ crore)", min: 1, max: 10000, step: 1 },
  { key: "MAX_SIGNALS_PER_DAY", label: "Max signals per day", min: 1, max: 500, step: 1 },
  { key: "POLL_INTERVAL_SECONDS", label: "Poll interval (seconds)", min: 5, max: 3600, step: 5 },
  { key: "PORTFOLIO_VALUE", label: "Portfolio value (₹)", min: 1000, max: 1e12, step: 10000 },
  { key: "DEFAULT_SL_PCT", label: "Default stop-loss (%)", min: 0.5, max: 50, step: 0.5 },
  { key: "DEFAULT_TARGET_RR", label: "Default target R:R", min: 0.5, max: 10, step: 0.5 },
  { key: "QUOTE_REFRESH_SECONDS", label: "Quote refresh (seconds)", min: 1, max: 600, step: 1 },
];

export function GlobalSettings() {
  const { data: settings, isLoading } = useGlobalSettings();
  const update = useUpdateSettings();
  const [values, setValues] = useState<Partial<GS>>({});
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (settings?.global) {
      setValues({ ...settings.global });
    }
  }, [settings]);

  const handleChange = (key: NumField, val: string) => {
    setValues((prev) => ({ ...prev, [key]: Number(val) }));
    setSaved(false);
  };

  const preLlmFilter = values.PRE_LLM_FILTER_ENABLED ?? true;

  const handleSave = async () => {
    setError(null);
    try {
      await update.mutateAsync({ global: values as GS });
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="widget" data-testid="global-settings">
      <h3>Risk &amp; Execution Settings</h3>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : (
        <>
          {FIELDS.map(({ key, label, min, max, step }) => (
            <div className="field" key={key}>
              <label htmlFor={`gs-${key}`}>{label}</label>
              <input
                id={`gs-${key}`}
                type="number"
                min={min}
                max={max}
                step={step}
                value={values[key] ?? ""}
                onChange={(e) => handleChange(key, e.target.value)}
              />
            </div>
          ))}
          <div className="field" style={{ marginTop: 4 }}>
            <label>Pre-LLM noise filter</label>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Toggle
                on={preLlmFilter}
                data-testid="pre-llm-filter-toggle"
                onChange={(next) => {
                  setValues((prev) => ({ ...prev, PRE_LLM_FILTER_ENABLED: next }));
                  setSaved(false);
                }}
              />
              <span className="field-hint" style={{ marginTop: 0 }}>
                {preLlmFilter
                  ? "Administrative filings (trading-window, compliance, newspaper notices) are skipped before the AI call."
                  : "Every filing is sent to the AI — including administrative noise (more cost, slower queue)."}
              </span>
            </div>
          </div>

          {error && <p className="pnl-neg">{error}</p>}
          {saved && <p className="pnl-pos">✓ Saved</p>}
          <button
            className="primary"
            onClick={handleSave}
            disabled={update.isPending}
            data-testid="save-settings"
          >
            {update.isPending ? "Saving…" : "Save Settings"}
          </button>
        </>
      )}
    </div>
  );
}
