// Settings → Server: restart the app or reboot the server from the UI.
// Two steps in place (no browser pop-up): pick, then type RESTART.
import { useState } from "react";

type What = "app" | "reboot";

export function ServerRestart() {
  const [what, setWhat] = useState<What | null>(null);
  const [typed, setTyped] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const go = async () => {
    if (!what) return;
    try {
      const r = await fetch("/api/system/restart", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: typed, what }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`);
      const back = Number(j.back_in_s ?? 20);
      setMsg(`${what === "reboot" ? "Rebooting the server" : "Restarting the app"} — back in about ${back} s. This page reloads by itself.`);
      setWhat(null);
      setTyped("");
      // Reload once the server answers again.
      const t0 = Date.now();
      const poll = async () => {
        try {
          const h = await fetch("/health", { cache: "no-store" });
          if (h.ok && Date.now() - t0 > 5000) { window.location.reload(); return; }
        } catch { /* still down */ }
        setTimeout(poll, 3000);
      };
      setTimeout(poll, 5000);
    } catch (e) {
      setMsg(`Not restarted: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    <div className="widget" data-testid="server-restart">
      <h3>Server</h3>
      <p className="text-dim" style={{ marginBottom: 8 }}>
        Restart if the app feels slow or the price feed is stuck. Orders already at the broker stay working; the bot re-arms its exits on start.
      </p>
      {!what ? (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button type="button" className="btn-sm" onClick={() => setWhat("app")} data-testid="restart-app">Restart app (~15 s)</button>
          <button type="button" className="btn-sm danger" onClick={() => setWhat("reboot")} data-testid="restart-reboot">Reboot server (~2 min)</button>
        </div>
      ) : (
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span>{what === "reboot" ? "Reboot the whole server?" : "Restart the app?"} Type RESTART:</span>
          <input value={typed} onChange={(e) => setTyped(e.target.value)} style={{ width: 110 }} autoFocus aria-label="Type RESTART to confirm" data-testid="restart-confirm-input" />
          <button type="button" className="btn-sm danger" disabled={typed !== "RESTART"} onClick={() => void go()} data-testid="restart-go">{what === "reboot" ? "Reboot" : "Restart"}</button>
          <button type="button" className="btn-sm" onClick={() => { setWhat(null); setTyped(""); }}>Cancel</button>
        </div>
      )}
      {msg && <p className="text-dim" style={{ marginTop: 8 }} data-testid="restart-msg">{msg}</p>}
    </div>
  );
}
