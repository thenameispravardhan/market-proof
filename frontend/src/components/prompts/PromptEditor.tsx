// Prompt editor: editable view of a single prompt template. Save
// bumps the version on the server (PUT /api/prompts/{event_type}).
// The user_template textarea has a hint about the {{pdf_url}}
// placeholder so operators know which variable is interpolated.

import { useEffect, useState } from "react";
import { useUpdatePrompt, usePrompts } from "../../hooks/useApi";

// DeepSeek models the operator can pick per template. The backend
// (PromptUpdate._model_check) accepts any non-empty string, so a
// model stored outside this list still renders — see `modelOptions`
// below, which folds the current value in if it isn't one of these.
const DEEPSEEK_MODELS: { value: string; label: string }[] = [
  { value: "deepseek-chat", label: "deepseek-chat — fast, general (default)" },
  { value: "deepseek-reasoner", label: "deepseek-reasoner — deeper, slower" },
  { value: "deepseek-coder", label: "deepseek-coder — code-focused" },
  { value: "deepseek-v4-flash", label: "deepseek-v4-flash — v4, fast" },
  { value: "deepseek-v4-pro", label: "deepseek-v4-pro — v4, most capable" },
];

export function PromptEditor({
  eventType,
  onSaved,
  onError,
}: {
  eventType: string | null;
  onSaved?: () => void;
  onError?: (msg: string) => void;
}) {
  const { data } = usePrompts();
  const update = useUpdatePrompt(eventType);

  const current = data?.find((p) => p.event_type === eventType) ?? null;

  const [systemPrompt, setSystemPrompt] = useState("");
  const [userTemplate, setUserTemplate] = useState("");
  const [model, setModel] = useState("deepseek-chat");
  const [temperature, setTemperature] = useState(0.2);
  const [maxTokens, setMaxTokens] = useState(2000);
  const [changeNote, setChangeNote] = useState("");

  // Re-seed the local form whenever the selected template changes.
  useEffect(() => {
    if (current) {
      setSystemPrompt(current.system_prompt);
      setUserTemplate(current.user_template);
      setModel(current.model);
      setTemperature(current.temperature);
      setMaxTokens(current.max_tokens);
      setChangeNote("");
    } else {
      setSystemPrompt("");
      setUserTemplate("");
      setModel("deepseek-chat");
      setTemperature(0.2);
      setMaxTokens(2000);
      setChangeNote("");
    }
  }, [current]);

  if (!eventType) {
    return (
      <div className="widget" data-testid="prompt-editor">
        <h3>Editor</h3>
        <p className="empty">Select a prompt from the list to edit it.</p>
      </div>
    );
  }
  if (!current) {
    return (
      <div className="widget" data-testid="prompt-editor">
        <h3>Editor</h3>
        <p className="empty">Loading template…</p>
      </div>
    );
  }

  const dirty =
    systemPrompt !== current.system_prompt ||
    userTemplate !== current.user_template ||
    model !== current.model ||
    temperature !== current.temperature ||
    maxTokens !== current.max_tokens;

  // The known models plus the current value if it isn't one of them,
  // so a model set outside this list (the backend allows any string)
  // still shows in the dropdown rather than silently resetting.
  const modelOptions = DEEPSEEK_MODELS.some((m) => m.value === model)
    ? DEEPSEEK_MODELS
    : [{ value: model, label: `${model} (custom)` }, ...DEEPSEEK_MODELS];

  const handleSave = async () => {
    try {
      await update.mutateAsync({
        system_prompt: systemPrompt,
        user_template: userTemplate,
        model,
        temperature,
        max_tokens: maxTokens,
        change_note: changeNote || undefined,
      });
      onSaved?.();
    } catch (e) {
      onError?.((e as Error).message || "Save failed");
    }
  };

  return (
    <div className="widget" data-testid="prompt-editor">
      <h3>
        Editor — {current.event_type}{" "}
        <span className="meta mono" style={{ color: "var(--text-dim)" }}>v{current.version}</span>
      </h3>
      <div className="field">
        <label htmlFor="sys-prompt">System prompt</label>
        <textarea
          id="sys-prompt"
          rows={6}
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="usr-tpl">User template</label>
        <textarea
          id="usr-tpl"
          rows={8}
          value={userTemplate}
          onChange={(e) => setUserTemplate(e.target.value)}
        />
        <div className="meta" style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 4 }}>
          Use <code className="mono">{"{{pdf_url}}"}</code> where the filing URL should be inserted.
        </div>
      </div>
      <div className="field">
        <label htmlFor="model">Model</label>
        <select
          id="model"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          data-testid="prompt-model"
        >
          {modelOptions.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          ))}
        </select>
        <div className="meta" style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 4 }}>
          The model used for this template's DeepSeek call. Saving bumps the version.
        </div>
      </div>
      <div className="field-row">
        <div className="field">
          <label htmlFor="temp">Temperature</label>
          <input
            id="temp"
            type="number"
            min={0}
            max={2}
            step={0.05}
            value={temperature}
            onChange={(e) => setTemperature(parseFloat(e.target.value) || 0)}
          />
        </div>
        <div className="field">
          <label htmlFor="mtok">Max tokens</label>
          <input
            id="mtok"
            type="number"
            min={64}
            max={8000}
            step={64}
            value={maxTokens}
            onChange={(e) => setMaxTokens(parseInt(e.target.value, 10) || 64)}
          />
        </div>
      </div>
      <div className="field">
        <label htmlFor="note">Change note (optional)</label>
        <input
          id="note"
          type="text"
          placeholder="e.g. tighten rationale constraint"
          value={changeNote}
          onChange={(e) => setChangeNote(e.target.value)}
        />
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <button
          className="primary"
          onClick={handleSave}
          disabled={!dirty || update.isPending}
          data-testid="save-prompt"
        >
          {update.isPending ? "Saving…" : "Save (bumps version)"}
        </button>
        {update.isError && (
          <span className="pnl-neg">{(update.error as Error).message}</span>
        )}
        {update.isSuccess && <span className="pnl-pos">Saved.</span>}
      </div>
    </div>
  );
}
