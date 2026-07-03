// Dashboard master switch for AI news analysis. When OFF, announcements
// are still monitored and stored, but nothing is sent to the LLM — no
// analyses, no signals, no auto trades. Manual trading from the Trade
// page is unaffected. Backed by the AI_ANALYSIS_ENABLED global setting
// (hot-applied — no restart needed).

import { useGlobalSettings, useUpdateSettings } from "../../hooks/useApi";

export function AiAnalysisToggle() {
  const { data } = useGlobalSettings();
  const update = useUpdateSettings();

  const loaded = data?.global !== undefined;
  const enabled = data?.global?.AI_ANALYSIS_ENABLED ?? true;

  return (
    <button
      className={`acct-switch ${enabled ? "on" : "off"}`}
      onClick={() => update.mutate({ global: { AI_ANALYSIS_ENABLED: !enabled } })}
      disabled={!loaded || update.isPending}
      title={
        enabled
          ? "AI news analysis is ON — filings are analyzed and can trigger auto trades. Click to pause."
          : "AI news analysis is OFF — news is still collected but not analyzed; no new auto trades. Click to resume."
      }
      data-testid="toggle-ai-analysis"
    >
      <span className="acct-switch-name">AI Analysis</span>
      <span className="acct-switch-track">
        <span className="acct-switch-knob" />
      </span>
      <span className="acct-switch-state">{enabled ? "ON" : "OFF"}</span>
    </button>
  );
}
