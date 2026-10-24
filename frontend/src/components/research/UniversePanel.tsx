/**
 * B5: point-in-time index membership (app/research/universe.py) — what each
 * index's history rests on, and the ways to extend it: record a list (fetched,
 * or pasted when the site turns the server away), import the change log.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";

type Cov = { index: string; label: string; snapshots: number; first_snapshot: string | null; last_snapshot: string | null;
  changes: number; first_change: string | null; trusted_from: string | null; members_now: number; list_url?: string };

const d8 = (s: string | null) => (s ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}` : "—");

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${res.status}`);
  return j as T;
}

export function UniversePanel() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["algo", "research", "universe"], queryFn: () => api.get<{ indexes: Cov[] }>("/api/algo/research/universe") });
  const [open, setOpen] = useState<string | null>(null);
  const [csv, setCsv] = useState("");
  const [pasteFor, setPasteFor] = useState<string | null>(null);
  const [pasted, setPasted] = useState("");
  const done = () => qc.invalidateQueries({ queryKey: ["algo", "research", "universe"] });
  const snap = useMutation({ mutationFn: (index: string) => post<Cov>(`/api/algo/research/universe/${index}/snapshot`, {}), onSuccess: done });
  const paste = useMutation({ mutationFn: (index: string) => post<Cov>(`/api/algo/research/universe/${index}/snapshot`, { symbols: pasted }),
    onSuccess: () => { setPasted(""); setPasteFor(null); done(); } });
  const imp = useMutation({ mutationFn: (index: string) => post<Cov & { added: number }>(`/api/algo/research/universe/${index}/changes`, { csv }),
    onSuccess: () => { setCsv(""); done(); } });
  const err = (snap.error || imp.error || paste.error) as Error | null;
  const pasteCov = list.data?.indexes.find((c) => c.index === pasteFor);
  return (
    <div className="widget widget-wide" data-testid="universe-panel" style={{ marginBottom: 12 }}>
      <h3>Point-in-time universes <span className="meta">each index as it was on each day — what a ranking backtest should choose from</span></h3>
      <div className="meta" style={{ marginBottom: 6 }}>
        Built from the lists recorded so far (the collector saves one each week) and the change log of inclusions / exclusions
        (NSE's index-change announcements, by effective date). Before “holds from”, membership assumes no changes.
      </div>
      {err && <div className="pnl-neg">{err.message}</div>}
      <table>
        <thead><tr><th>Index</th><th>Lists recorded</th><th>Change log</th><th>Holds from</th><th>Members today</th><th></th></tr></thead>
        <tbody>{(list.data?.indexes ?? []).map((c) => (
          <tr key={c.index} data-testid={`universe-${c.index}`}>
            <td>{c.label}</td>
            <td className="mono">{c.snapshots ? `${c.snapshots} · ${d8(c.first_snapshot)} → ${d8(c.last_snapshot)}` : "none"}</td>
            <td className="mono">{c.changes ? `${c.changes} · from ${d8(c.first_change)}` : "none"}</td>
            <td className="mono">{d8(c.trusted_from)}</td>
            <td className="mono">{c.members_now || "—"}</td>
            <td style={{ whiteSpace: "nowrap" }}>
              <button type="button" className="btn-sm" disabled={snap.isPending} onClick={() => snap.mutate(c.index)}>record today's list</button>{" "}
              <button type="button" className="btn-sm" onClick={() => { setPasteFor(pasteFor === c.index ? null : c.index); setOpen(null); }}>paste list</button>{" "}
              <button type="button" className="btn-sm" onClick={() => { setOpen(open === c.index ? null : c.index); setPasteFor(null); }}>import changes</button>
            </td>
          </tr>))}
        </tbody>
      </table>
      {pasteCov && (
        <div style={{ display: "grid", gap: 6, marginTop: 8 }} data-testid="universe-paste">
          <span className="meta">Today's {pasteCov.label} list: download{" "}
            {pasteCov.list_url ? <a href={pasteCov.list_url} target="_blank" rel="noreferrer">the constituent file</a> : "the constituent file"}{" "}
            in your browser and paste its contents (or just the tickers, one per line).</span>
          <textarea rows={6} value={pasted} onChange={(e) => setPasted(e.target.value)} data-testid="universe-paste-text"
            placeholder={"Company Name,Industry,Symbol,Series,ISIN Code\n…  — or —\nRELIANCE\nHDFCBANK\n…"} style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }} />
          <div><button type="button" className="primary" disabled={!pasted.trim() || paste.isPending} onClick={() => paste.mutate(pasteCov.index)}>Record</button></div>
        </div>
      )}
      {open && (
        <div style={{ display: "grid", gap: 6, marginTop: 8 }}>
          <span className="meta">Change log for {list.data?.indexes.find((c) => c.index === open)?.label}: one row per stock, <code>date,symbol,action</code> — the effective date (YYYY-MM-DD), the NSE ticker, add or remove.</span>
          <textarea rows={6} value={csv} onChange={(e) => setCsv(e.target.value)} data-testid="universe-csv"
            placeholder={"date,symbol,action\n2024-03-28,SHRIRAMFIN,add\n2024-03-28,UPL,remove"} style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }} />
          <div><button type="button" className="primary" disabled={!csv.trim() || imp.isPending} onClick={() => imp.mutate(open)}>Import</button>
            {imp.data && <span className="meta"> {imp.data.added} new changes, {imp.data.changes} in the log.</span>}</div>
        </div>
      )}
    </div>
  );
}
