/**
 * B6: nightly re-validation (app/research/revalidate.py) — its switch, the last
 * run, and the strategies flagged because their edge no longer holds.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Toggle } from "../common/Toggle";

type Flag = { id: number; strategy_id: number; name: string | null; card_id: number | null; reason: string; created_at: string | null };
type Nightly = { enabled: boolean; time: string; last_run?: number;
  last?: { strategies: number; flagged: number; new_flags: number; elapsed_s: number }; flags: Flag[] };

async function send<T>(url: string, method: string, body: unknown): Promise<T> {
  const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${res.status}`);
  return j as T;
}

export function NightlyPanel({ onOpenCard }: { onOpenCard: (id: number) => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["algo", "research", "nightly"], queryFn: () => api.get<Nightly>("/api/algo/research/nightly") });
  const done = () => qc.invalidateQueries({ queryKey: ["algo", "research"] });
  const cfg = useMutation({ mutationFn: (patch: Partial<Nightly>) => send("/api/algo/research/nightly", "PUT", patch), onSuccess: done });
  const run = useMutation({ mutationFn: () => send<{ strategies: number; new_flags: number }>("/api/algo/research/nightly/run", "POST", {}), onSuccess: done });
  const clear = useMutation({ mutationFn: (p: { id: number; note: string }) => send(`/api/algo/research/flags/${p.id}/clear`, "POST", { note: p.note }), onSuccess: done });
  const [time, setTime] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<number, string>>({});
  const n = q.data;
  const err = (cfg.error || run.error || clear.error) as Error | null;
  return (
    <div className="widget widget-wide" data-testid="nightly-panel" style={{ marginBottom: 12 }}>
      <h3>Nightly re-validation <span className="meta">does every switched-on strategy still clear the bar?</span></h3>
      <div className="algo-operand" style={{ gap: 10 }}>
        <Toggle on={!!n?.enabled} disabled={!n || cfg.isPending} onChange={(on) => cfg.mutate({ enabled: on })} label="every weekday at" data-testid="nightly-toggle" />
        <input value={time ?? n?.time ?? "18:30"} onChange={(e) => setTime(e.target.value)} onBlur={() => { if (time && time !== n?.time) cfg.mutate({ time }); }} style={{ width: 56 }} data-testid="nightly-time" />
        <span className="meta">IST</span>
        <button type="button" className="btn-sm" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Running…" : "run now"}</button>
        <span className="meta">
          {n?.last_run ? `last: ${new Date(n.last_run * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "short", timeStyle: "short" })} · ${n.last?.strategies ?? 0} strategies, ${n.last?.flagged ?? 0} flagged` : "never run"}
        </span>
      </div>
      <div className="meta" style={{ margin: "6px 0" }}>
        Each switched-on strategy (paper or live) gets its research check again on its card's window rolled to today, and its trades
        since that card — data the card never saw. Flagged when it is killed, has lost money since its card, or trades LIVE without one.
        The live desk's auto-pause reads the open flags; nothing here switches a strategy off by itself.
      </div>
      {err && <div className="pnl-neg">{err.message}</div>}
      {n && n.flags.length > 0 ? (
        <table data-testid="nightly-flags">
          <thead><tr><th>Strategy</th><th>Since</th><th>Why</th><th></th></tr></thead>
          <tbody>{n.flags.map((f) => (
            <tr key={f.id}>
              <td>{f.name ?? `#${f.strategy_id}`}</td>
              <td className="mono">{f.created_at ? new Date(f.created_at).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" }) : "—"}</td>
              <td className="pnl-neg">{f.reason}{f.card_id ? <> · <button type="button" className="btn-sm ghost" onClick={() => onOpenCard(f.card_id!)}>card #{f.card_id}</button></> : null}</td>
              <td style={{ whiteSpace: "nowrap" }}>
                <input placeholder="what was decided" value={notes[f.id] ?? ""} onChange={(e) => setNotes({ ...notes, [f.id]: e.target.value })} style={{ width: 160 }} />{" "}
                <button type="button" className="btn-sm" disabled={clear.isPending} onClick={() => clear.mutate({ id: f.id, note: notes[f.id] ?? "" })}>clear</button>
              </td>
            </tr>))}
          </tbody>
        </table>
      ) : n && <div className="meta">No open flags.</div>}
    </div>
  );
}
