// GlobalSettings — editable global risk settings form.
import { useEffect, useState } from "react";
import { useGlobalSettings, useUpdateSettings } from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";
import type { GlobalSettings as GS } from "../../types";

type NumField = Exclude<
  keyof GS,
  | "TRADING_MODE"
  | "PRE_LLM_FILTER_ENABLED"
  | "AI_ANALYSIS_ENABLED"
  | "SEND_EXTRACTED_TEXT"
  | "FAST_TRACK_ENABLED"
  | "OUTCOME_LOGGER_ENABLED"
  | "NEWS_AGE_FROM_RECEIPT"
  // News-source toggles (edited on the News Sources card, not here).
  | "NSE_API_ENABLED"
  | "BSE_API_ENABLED"
  | "NSE_RSS_ENABLED"
  // Exit Manager keys (edited on the Exits page, not here).
  | "ATR_ENABLED"
  | "BREAKEVEN_ENABLED"
  | "SCALE_OUT_ENABLED"
  | "CONSOLIDATION_EXIT_ENABLED"
  | "STALL_EXIT_ENABLED"
  | "SQUARE_OFF_TIME_IST"
>;

const FIELDS: { key: NumField; label: string; min: number; max: number; step: number }[] = [
  { key: "MAX_CAPITAL_RISK_PCT", label: "Max capital risk (%)", min: 0.1, max: 100, step: 0.1 },
  { key: "DAILY_MAX_LOSS_PCT", label: "Daily max loss (%)", min: 0.1, max: 100, step: 0.1 },
  { key: "MAX_CONCURRENT_POSITIONS", label: "Max concurrent positions", min: 1, max: 100, step: 1 },
  { key: "MAX_SINGLE_POSITION_PCT", label: "Max single position (%)", min: 0.1, max: 100, step: 0.1 },
  { key: "INTRADAY_LEVERAGE", label: "Intraday leverage (×)", min: 1, max: 10, step: 0.5 },
  { key: "MIN_LIQUIDITY_CRORE", label: "Min liquidity (₹ crore)", min: 1, max: 10000, step: 1 },
  { key: "MAX_SIGNALS_PER_DAY", label: "Max signals per day", min: 1, max: 500, step: 1 },
  { key: "POLL_INTERVAL_SECONDS", label: "Poll interval (seconds)", min: 5, max: 3600, step: 5 },
  { key: "PORTFOLIO_VALUE", label: "Portfolio value (₹)", min: 1000, max: 1e12, step: 10000 },
  { key: "DEFAULT_SL_PCT", label: "Default stop-loss (%)", min: 0.5, max: 50, step: 0.5 },
  { key: "DEFAULT_TARGET_RR", label: "Default target R:R", min: 0.5, max: 10, step: 0.5 },
  { key: "QUOTE_REFRESH_SECONDS", label: "Quote refresh (seconds)", min: 1, max: 600, step: 1 },
  { key: "LLM_MAX_TOKENS", label: "AI max output tokens", min: 100, max: 4000, step: 50 },
  { key: "MAX_NEWS_AGE_SECONDS", label: "Max news age (seconds)", min: 5, max: 600, step: 5 },
  { key: "MAX_NEWS_AGE_ABSOLUTE_SECONDS", label: "Absolute news age ceiling (seconds, 0 = off)", min: 0, max: 86400, step: 60 },
  { key: "PIPELINE_DEADLINE_SECONDS", label: "Signal deadline (seconds, 0 = off)", min: 0, max: 300, step: 1 },
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
  // Default OFF — legacy URL/metadata mode until the operator opts in.
  const sendExtractedText = values.SEND_EXTRACTED_TEXT ?? false;
  // Default OFF — everything takes the AI track until the operator opts in.
  const fastTrack = values.FAST_TRACK_ENABLED ?? false;
  // Default ON — passive telemetry, no trading influence.
  const outcomeLogger = values.OUTCOME_LOGGER_ENABLED ?? true;
  // Default OFF — legacy filed_at clock until the operator opts in.
  const newsAgeFromReceipt = values.NEWS_AGE_FROM_RECEIPT ?? false;

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
            <label>Measure news age from receipt</label>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Toggle
                on={newsAgeFromReceipt}
                data-testid="news-age-from-receipt-toggle"
                onChange={(next) => {
                  setValues((prev) => ({ ...prev, NEWS_AGE_FROM_RECEIPT: next }));
                  setSaved(false);
                }}
              />
              <span className="field-hint" style={{ marginTop: 0 }}>
                {newsAgeFromReceipt
                  ? "Age = time since the bot first SAW the filing. The exchange's own publish lag no longer counts against you — until it's published, nobody could trade it. The absolute ceiling above still rejects genuinely old news."
                  : "Age = time since the exchange's stated filing time, which INCLUDES the exchange's publish lag (measured median ~35s) — filings get dropped as stale for a delay the bot didn't cause."}
              </span>
            </div>
          </div>
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
          <div className="field" style={{ marginTop: 4 }}>
            <label>Send extracted PDF text to AI</label>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Toggle
                on={sendExtractedText}
                data-testid="send-extracted-text-toggle"
                onChange={(next) => {
                  setValues((prev) => ({ ...prev, SEND_EXTRACTED_TEXT: next }));
                  setSaved(false);
                }}
              />
              <span className="field-hint" style={{ marginTop: 0 }}>
                {sendExtractedText
                  ? "The filing PDF is downloaded and its relevant pages (Hindi removed) are sent to the AI as real text. Falls back to URL mode if extraction fails."
                  : "Legacy mode: the AI gets only the PDF URL + headline metadata (no filing text)."}
              </span>
            </div>
          </div>
          <div className="field" style={{ marginTop: 4 }}>
            <label>Deterministic fast track</label>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Toggle
                on={fastTrack}
                data-testid="fast-track-toggle"
                onChange={(next) => {
                  setValues((prev) => ({ ...prev, FAST_TRACK_ENABLED: next }));
                  setSaved(false);
                }}
              />
              <span className="field-hint" style={{ marginTop: 0 }}>
                {fastTrack
                  ? "High-conviction headlines (order win / buyback with explicit ₹-crore value, key-management resignation) skip the AI and hit the rules engine in milliseconds. Everything else still goes to the AI."
                  : "Every filing takes the AI track (legacy behavior)."}
              </span>
            </div>
          </div>
          <div className="field" style={{ marginTop: 4 }}>
            <label>Signal outcome tracking</label>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Toggle
                on={outcomeLogger}
                data-testid="outcome-logger-toggle"
                onChange={(next) => {
                  setValues((prev) => ({ ...prev, OUTCOME_LOGGER_ENABLED: next }));
                  setSaved(false);
                }}
              />
              <span className="field-hint" style={{ marginTop: 0 }}>
                {outcomeLogger
                  ? "Every signal's price move at +5 and +30 minutes is recorded (win-rate report + future ML training data). Pure telemetry — never affects trading."
                  : "No outcome tracking — win-rate review and ML training data will not accumulate."}
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
