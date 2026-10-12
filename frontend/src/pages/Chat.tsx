// SLM chat — talk to the fine-tuned model (v3) the bot shadow-scores with.
//
// One message = one filing: the first line is the headline, the rest is the
// filing text. The server renders it in the exact training prompt and returns
// the model's JSON plus the bot's mapping of it. Read-only: nothing here can
// reach the rules engine or place an order.

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";

type Mapped = {
  recommendation: string; sentiment: string; sentiment_score: number;
  confidence: number; event_type: string; reasoning: string;
};
type Reply = { model: string; latency_ms: number; prompt: string; content: string; mapped?: Mapped; error?: string };
type Turn = { symbol: string; message: string; reply?: Reply; error?: string };

const tone = (r: string) => (r === "BUY" ? "pnl-pos" : r === "SELL" ? "pnl-neg" : "meta");

export default function Chat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [symbol, setSymbol] = useState("");
  const [message, setMessage] = useState("");
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
    <div>
      <div className="dashboard-head">
        <h1 className="page-title">SLM Chat</h1>
        <span className="meta">first line = headline, the rest = filing text · Ctrl+Enter sends</span>
      </div>

      {turns.map((t, i) => (
        <div key={i} className="widget widget-wide" style={{ marginBottom: 12 }}>
          <div className="meta">{t.symbol || "—"}</div>
          <div style={{ whiteSpace: "pre-wrap", marginBottom: 8 }}>{t.message}</div>
          {t.error && <div className="pnl-neg">{t.error}</div>}
          {!t.reply && !t.error && <div className="meta">thinking…</div>}
          {t.reply && (
            <>
              {t.reply.mapped ? (
                <h3>
                  <span className={tone(t.reply.mapped.recommendation)}>{t.reply.mapped.recommendation}</span>{" "}
                  <span className="meta">
                    {t.reply.mapped.event_type} · {t.reply.mapped.sentiment} {t.reply.mapped.sentiment_score} ·
                    confidence {t.reply.mapped.confidence} · {t.reply.model} · {(t.reply.latency_ms / 1000).toFixed(1)} s
                  </span>
                </h3>
              ) : (
                <div className="pnl-neg">{t.reply.error}</div>
              )}
              {t.reply.mapped && <div style={{ marginBottom: 6 }}>{t.reply.mapped.reasoning}</div>}
              <details>
                <summary className="meta">raw reply + prompt sent</summary>
                <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>{t.reply.content}</pre>
                <pre className="mono meta" style={{ whiteSpace: "pre-wrap" }}>{t.reply.prompt}</pre>
              </details>
            </>
          )}
        </div>
      ))}
      {turns.length === 0 && <div className="empty">Paste a filing headline (and optionally its text) to see what v3 says.</div>}

      <div className="widget widget-wide">
        <input aria-label="Symbol" value={symbol} placeholder="SYMBOL (optional, e.g. NCC)"
               onChange={(e) => setSymbol(e.target.value)} style={{ width: 220, marginBottom: 8 }} />
        <textarea aria-label="Filing" value={message} rows={6} style={{ width: "100%" }}
                  placeholder={"NCC Limited has informed the Exchange regarding receipt of orders worth Rs 2,107 crore\n(optional) paste the filing text here"}
                  onChange={(e) => setMessage(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit(); }} />
        <button className="primary" onClick={submit} disabled={send.isPending || !message.trim()}>
          {send.isPending ? "Asking v3…" : "Send"}
        </button>
      </div>
    </div>
  );
}
