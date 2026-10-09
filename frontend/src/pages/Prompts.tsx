// Prompts page — SINGLE-PROMPT mode. There's one prompt (the DEFAULT
// template) that drives the AI analysis of every news item; this page
// just edits that one prompt. (The per-event-type templates still exist
// in the DB but are no longer used by the analyzer.)

import { PromptEditor } from "../components/prompts/PromptEditor";
import { PromptHistory } from "../components/prompts/PromptHistory";
import { PromptPreview } from "../components/prompts/PromptPreview";
import { useGlobalSettings } from "../hooks/useApi";

// The one prompt the analyzer uses for every announcement.
const ANALYSIS_PROMPT = "DEFAULT";

export default function Prompts() {
  const { data: settings } = useGlobalSettings();
  const usingSlm = settings?.global?.LLM_PROVIDER === "slm";
  const tokenCap = settings?.global?.LLM_MAX_TOKENS;

  return (
    <div>
      <h1 className="page-title">Analysis Prompt</h1>
      <p className="text-dim" style={{ marginBottom: 16, maxWidth: 720 }}>
        This prompt is sent to DeepSeek for <strong>every</strong> news item.
        Each save creates a new version, kept in the history below so you can
        roll back.
      </p>
      {usingSlm && (
        <p className="pnl-neg" style={{ marginBottom: 16, maxWidth: 720 }} data-testid="prompt-slm-notice">
          Filings are currently read by your own fine-tuned model (Settings → AI
          analysis → AI model). It uses its own built-in prompt, so changes
          here have no effect until you switch that back to DeepSeek.
        </p>
      )}
      <PromptEditor eventType={ANALYSIS_PROMPT} tokenCap={tokenCap} />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
          gap: 16,
          marginTop: 16,
        }}
      >
        <PromptHistory eventType={ANALYSIS_PROMPT} />
        <PromptPreview eventType={ANALYSIS_PROMPT} />
      </div>
    </div>
  );
}
