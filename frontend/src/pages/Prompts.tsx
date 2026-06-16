// Prompts page — SINGLE-PROMPT mode. There's one prompt (the DEFAULT
// template) that drives the AI analysis of every news item; this page
// just edits that one prompt. (The per-event-type templates still exist
// in the DB but are no longer used by the analyzer.)

import { PromptEditor } from "../components/prompts/PromptEditor";
import { PromptHistory } from "../components/prompts/PromptHistory";
import { PromptPreview } from "../components/prompts/PromptPreview";

// The one prompt the analyzer uses for every announcement.
const ANALYSIS_PROMPT = "DEFAULT";

export default function Prompts() {
  return (
    <div>
      <h1 className="page-title">Analysis Prompt</h1>
      <p className="text-dim" style={{ marginBottom: 16, maxWidth: 720 }}>
        This single prompt is sent to DeepSeek for <strong>every</strong> news
        item. Edit it here; each save bumps the version and is kept in the
        history below. The detected event type (e.g. ORDER_WIN) is injected
        into the prompt automatically for context.
      </p>
      <PromptEditor eventType={ANALYSIS_PROMPT} />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
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
