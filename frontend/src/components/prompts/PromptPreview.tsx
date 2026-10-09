// Prompt preview: run the *current* (unsaved) template against a fake
// PDF URL by POSTing to /api/prompts/{event_type}/preview. The server
// is expected to return { output, tokens_used, model }; we render the
// output in a <pre> for readability.

import { useState } from "react";
import { usePreviewPrompt } from "../../hooks/useApi";

const SAMPLE_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/SAMPLE.pdf";

export function PromptPreview({ eventType }: { eventType: string | null }) {
  const [pdfUrl, setPdfUrl] = useState(SAMPLE_URL);
  const preview = usePreviewPrompt(eventType);

  const handleRun = async () => {
    if (!eventType) return;
    try {
      await preview.mutateAsync(pdfUrl);
    } catch {
      // surfaced via preview.isError
    }
  };

  return (
    <div className="widget" data-testid="prompt-preview">
      <h3>Preview</h3>
      <p className="field-hint" style={{ marginBottom: 8 }}>
        Shows the <strong>saved</strong> prompt with a filing URL filled in.
        Nothing is sent to the AI. Save your edits first to preview them.
      </p>
      {!eventType ? (
        <p className="empty">Select a prompt to preview.</p>
      ) : (
        <>
          <div className="field">
            <label htmlFor="prev-url">Example filing URL</label>
            <input
              id="prev-url"
              type="text"
              value={pdfUrl}
              onChange={(e) => setPdfUrl(e.target.value)}
            />
          </div>
          <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
            <button
              className="primary"
              onClick={handleRun}
              disabled={preview.isPending || !pdfUrl}
              data-testid="run-preview"
            >
              {preview.isPending ? "Running…" : "Run preview"}
            </button>
            {preview.isError && (
              <span className="pnl-neg">{(preview.error as Error).message}</span>
            )}
          </div>
          {preview.data && (
            <pre
              className="mono"
              style={{
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                background: "var(--bg-input)",
                border: "1px solid var(--border)",
                borderRadius: 0,
                padding: 8,
                maxHeight: 240,
                overflow: "auto",
                fontSize: 12.5,
              }}
            >
              {preview.data.rendered_user_template}
            </pre>
          )}
        </>
      )}
    </div>
  );
}
