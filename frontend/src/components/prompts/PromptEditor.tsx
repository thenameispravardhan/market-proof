// Prompt editor: editable view of a single prompt template. Save
// bumps the version on the server (PUT /api/prompts/{event_type}).
// The user_template textarea has a hint about the {{pdf_url}}
// placeholder so operators know which variable is interpolated.

import { useEffect, useState } from "react";
import { useUpdatePrompt, usePrompts } from "../../hooks/useApi";

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
  const [temperature, setTemperature] = useState(0.2);
  const [maxTokens, setMaxTokens] = useState(2000);
  const [changeNote, setChangeNote] = useState("");

  // Re-seed the local form whenever the selected template changes.
  useEffect(() => {
    if (current) {
      setSystemPrompt(current.system_prompt);
      setUserTemplate(current.user_template);
      setTemperature(current.temperature);
      setMaxTokens(current.max_tokens);
      setChangeNote("");
    } else {
      setSystemPrompt("");
      setUserTemplate("");
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
    temperature !== current.temperature ||
    maxTokens !== current.max_tokens;

  const handleSave = async () => {
    try {
      await update.mutateAsync({
        system_prompt: systemPrompt,
        user_template: userTemplate,
        model: current.model,
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
