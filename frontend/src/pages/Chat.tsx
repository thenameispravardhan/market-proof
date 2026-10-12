// SLM chat — a ChatGPT-style window onto the fine-tuned model (v3).
//
// Each message is read as one filing: first line = headline, the rest =
// filing text (v3 was trained on single filings, so there is no memory
// between messages). The server renders the exact training prompt and
// returns the model's JSON plus the bot's mapping. Read-only: nothing here
// can reach the rules engine or place an order.

import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";

type Mapped = {
  recommendation: string; sentiment: string; sentiment_score: number;
  confidence: number; event_type: string; reasoning: string; summary: string;
};
type Reply = { model: string; latency_ms: number; prompt: string; content: string; mapped?: Mapped; error?: string };
type Turn = { symbol: string; message: string; reply?: Reply; error?: string };

const tone = (r: string) => (r === "BUY" ? "var(--pos, #16a34a)" : r === "SELL" ? "var(--neg, #dc2626)" : "var(--muted, #888)");
const bubble = (mine: boolean): React.CSSProperties => ({
  maxWidth: "78%", padding: "10px 14px", borderRadius: 16, whiteSpace: "pre-wrap", lineHeight: 1.5,
  alignSelf: mine ? "flex-end" : "flex-start",
  background: mine ? "var(--accent, #2563eb)" : "var(--panel, rgba(127,127,127,.12))",
  color: mine ? "#fff" : "inherit",
});

function Answer({ r }: { r: Reply }) {
  const m = r.mapped;
  if (!m) return <span style={{ color: tone("SELL") }}>{r.error || "The model did not return valid JSON."}</span>;
  return (
    <>
      <div>
        <b style={{ color: tone(m.recommendation) }}>{m.recommendation}</b>
        {" — "}{m.event_type.replace(/_/g, " ").toLowerCase()}, {m.sentiment} ({m.sentiment_score}), confidence {m.confidence}
      </div>
      <div style={{ marginTop: 6 }}>{m.reasoning.replace(/^tradebot-slm-v3: /, "")}</div>
      <details style={{ marginTop: 8 }}>
        <summary className="meta" style={{ cursor: "pointer" }}>{r.model} · {(r.latency_ms / 1000).toFixed(1)} s · raw reply + prompt</summary>
        <pre className="mono" style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{r.content}</pre>
        <pre className="mono meta" style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{r.prompt}</pre>
      </details>
    </>
  );
}

export default function Chat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [symbol, setSymbol] = useState("");
  const [message, setMessage] = useState("");
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [turns]);  // newer browsers return a Promise; never hand it to React

  const send = useMutation({
    mutationFn: (t: Turn) => api.post<Reply>("/api/model/slm-chat", { symbol: t.symbol, message: t.message }),
    onSuccess: (reply, t) => setTurns((xs) => xs.map((x) => (x === t ? { ...x, reply } : x))),
    onError: (e: Error, t) => setTurns((xs) => xs.map((x) => (x === t ? { ...x, error: e.message } : x))),
  });
  const submit = () => {
    if (!message.trim() || send.isPending) return;
    const t: Turn = { symbol: symbol.trim(), message: message.trim() };
    setTurns((xs) => [...xs, t]);
    setMessage("");
    send.mutate(t);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "calc(100vh - 120px)", maxWidth: 860, margin: "0 auto" }}>
      <div style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 12, padding: "12px 4px" }}>
        {turns.length === 0 && (
          <div style={{ margin: "auto", textAlign: "center" }} className="meta">
            <h2 style={{ marginBottom: 8 }}>Ask the SLM about a filing</h2>
            Paste a headline (and optionally the filing text below it).<br />
            Enter sends · Shift+Enter for a new line
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={bubble(true)}>{t.symbol && <b>{t.symbol.toUpperCase()} · </b>}{t.message}</div>
            <div style={bubble(false)}>
              {t.error ? <span style={{ color: tone("SELL") }}>{t.error}</span>
                : t.reply ? <Answer r={t.reply} />
                : <span className="meta">thinking…</span>}
            </div>
          </div>
        ))}
        <div ref={end} />
      </div>

      <div style={{ display: "flex", gap: 8, alignItems: "flex-end", padding: 10, borderRadius: 18,
                    border: "1px solid var(--border, rgba(127,127,127,.35))" }}>
        <input aria-label="Symbol" value={symbol} placeholder="Symbol" onChange={(e) => setSymbol(e.target.value)}
               style={{ width: 90, border: "none", background: "transparent", outline: "none" }} />
        <textarea aria-label="Message" value={message} rows={Math.min(8, message.split("\n").length)}
                  placeholder="Message the SLM…" onChange={(e) => setMessage(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
                  style={{ flex: 1, resize: "none", border: "none", background: "transparent", outline: "none", font: "inherit" }} />
        <button className="primary" aria-label="Send" onClick={submit} disabled={send.isPending || !message.trim()}
                style={{ borderRadius: 12, minWidth: 40 }}>
          {send.isPending ? "…" : "↑"}
        </button>
      </div>
    </div>
  );
}
