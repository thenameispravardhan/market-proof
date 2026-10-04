// ChartWidgets — the right-panel widgets the active chart renders into the
// dock: Data Window (values under the crosshair), Object Tree (series,
// indicators, drawings by pane) and the Alerts manager (+ log).

import { useState } from "react";
import { describe, type AlertItem } from "./alerts";

export interface DataRow {
  label: string;
  value: string;
  color?: string;
}

export function DataWindow({ title, groups }: { title: string; groups: { title: string; rows: DataRow[] }[] }) {
  return (
    <div className="dw" data-testid="data-window">
      <div className="dw-title">{title}</div>
      {groups.map((g, i) => (
        <div key={i} className="dw-group">
          <div className="dw-gtitle">{g.title}</div>
          {g.rows.map((r, j) => (
            <div key={j} className="dw-row">
              <span>{r.label}</span>
              <b style={r.color ? { color: r.color } : undefined}>{r.value}</b>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

export interface TreeItem {
  id: string;
  kind: "series" | "indicator" | "drawing" | "compare";
  label: string;
  visible: boolean;
  locked?: boolean;
  selected?: boolean;
}

export function ObjectTree({
  panes,
  onVisible,
  onLock,
  onDelete,
  onRename,
  onSelect,
  onMove,
  onSettings,
}: {
  panes: { title: string; items: TreeItem[] }[];
  onVisible: (it: TreeItem) => void;
  onLock: (it: TreeItem) => void;
  onDelete: (it: TreeItem) => void;
  onRename: (it: TreeItem, name: string) => void;
  onSelect: (it: TreeItem) => void;
  onMove: (it: TreeItem, by: -1 | 1) => void;
  onSettings: (it: TreeItem) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [txt, setTxt] = useState("");
  return (
    <div className="otree" data-testid="object-tree">
      {panes.map((p, i) => (
        <div key={i} className="otree-pane">
          <div className="otree-ptitle">{p.title}</div>
          {p.items.length === 0 && <div className="hint">empty</div>}
          {p.items.map((it) => (
            <div key={`${it.kind}:${it.id}`} className={`otree-row${it.selected ? " on" : ""}${it.visible ? "" : " off"}`} onClick={() => onSelect(it)}>
              <span className="otree-kind">{it.kind === "series" ? "◆" : it.kind === "indicator" ? "ƒ" : it.kind === "compare" ? "⇄" : "✎"}</span>
              {editing === `${it.kind}:${it.id}` ? (
                <input
                  className="chart-menu-input"
                  autoFocus
                  value={txt}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => setTxt(e.target.value)}
                  onBlur={() => { onRename(it, txt); setEditing(null); }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") { onRename(it, txt); setEditing(null); }
                    if (e.key === "Escape") setEditing(null);
                  }}
                />
              ) : (
                <span className="otree-label" onDoubleClick={(e) => { if (it.kind !== "drawing") return; e.stopPropagation(); setTxt(it.label); setEditing(`${it.kind}:${it.id}`); }} title={it.kind === "drawing" ? "Double-click to rename" : undefined}>{it.label}</span>
              )}
              <span className="otree-acts" onClick={(e) => e.stopPropagation()}>
                {it.kind === "drawing" && <button type="button" title="Move up" onClick={() => onMove(it, 1)}>▲</button>}
                {it.kind === "drawing" && <button type="button" title="Move down" onClick={() => onMove(it, -1)}>▼</button>}
                {(it.kind === "drawing" || it.kind === "indicator") && <button type="button" title="Settings" onClick={() => onSettings(it)}>⚙</button>}
                {it.kind === "drawing" && <button type="button" title={it.locked ? "Unlock" : "Lock"} onClick={() => onLock(it)}>{it.locked ? "🔒" : "🔓"}</button>}
                <button type="button" title={it.visible ? "Hide" : "Show"} onClick={() => onVisible(it)}>{it.visible ? "👁" : "◌"}</button>
                {it.kind !== "series" && <button type="button" title="Remove" onClick={() => onDelete(it)}>✕</button>}
              </span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

export function AlertsPanel({
  symbolName,
  alerts,
  log,
  fmt,
  onCreate,
  onEdit,
  onToggle,
  onDelete,
  onClearLog,
}: {
  symbolName: string;
  alerts: AlertItem[];
  log: { id: number; ts: number; text: string }[];
  fmt: (n: number) => string;
  onCreate: () => void;
  onEdit: (a: AlertItem) => void;
  onToggle: (a: AlertItem) => void;
  onDelete: (a: AlertItem) => void;
  onClearLog: () => void;
}) {
  const [tab, setTab] = useState<"alerts" | "log">("alerts");
  return (
    <div className="alerts-panel" data-testid="alerts-panel">
      <div className="ap-head">
        <button type="button" className={`chip${tab === "alerts" ? " on" : ""}`} onClick={() => setTab("alerts")}>Alerts ({alerts.length})</button>
        <button type="button" className={`chip${tab === "log" ? " on" : ""}`} onClick={() => setTab("log")}>Log ({log.length})</button>
        <span className="grow" />
        <button type="button" className="cbtn" onClick={onCreate} data-testid="alerts-create">+ Create</button>
      </div>
      {tab === "alerts" ? (
        <div className="ap-list">
          {alerts.length === 0 && <div className="hint">No alerts on {symbolName}. Alt+A adds one at the cursor.</div>}
          {alerts.map((a) => (
            <div key={a.id} className={`ap-row${a.active ? "" : " off"}`}>
              <div className="ap-main" onClick={() => onEdit(a)} title="Edit">
                <b>{symbolName}</b> {describe(a, fmt)}
                {a.message && <div className="hint">{a.message}</div>}
                <div className="hint">{a.active ? "active" : a.fired ? `triggered ${a.fired}×` : "stopped"}{a.expires ? ` · expires ${new Date(a.expires).toLocaleString("en-IN")}` : ""}</div>
              </div>
              <button type="button" className="chart-menu-x" title={a.active ? "Pause" : "Resume"} onClick={() => onToggle(a)}>{a.active ? "⏸" : "▶"}</button>
              <button type="button" className="chart-menu-x" title="Delete" onClick={() => onDelete(a)} data-testid={`chart-alert-del-${a.id}`}>✕</button>
            </div>
          ))}
        </div>
      ) : (
        <div className="ap-list">
          {log.length === 0 && <div className="hint">Nothing has fired yet.</div>}
          {log.map((l) => (
            <div key={l.id} className="ap-row"><span className="hint">{new Date(l.ts).toLocaleTimeString("en-IN")}</span> {l.text}</div>
          ))}
          {log.length > 0 && <button type="button" className="cbtn" onClick={onClearLog}>Clear log</button>}
        </div>
      )}
    </div>
  );
}
