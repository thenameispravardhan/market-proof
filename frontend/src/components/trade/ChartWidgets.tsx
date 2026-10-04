// ChartWidgets — the right-panel widgets the active chart renders into the
// dock: Data Window (values under the crosshair), Object Tree (series,
// indicators, drawings by pane) and the Alerts manager (+ log).

import { useState, type DragEvent } from "react";
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
  /** Drawings: the group they belong to. */
  group?: string;
}

/** Where a dragged tree row was dropped. */
export type TreeDrop =
  | { kind: "item"; item: TreeItem }
  | { kind: "pane"; pane: number }
  | { kind: "group"; group: string }
  | { kind: "newpane" };

export type GroupAction = "hide" | "show" | "lock" | "unlock" | "ungroup" | "delete" | { rename: string };

const KIND_ICON: Record<TreeItem["kind"], string> = { series: "◆", indicator: "ƒ", compare: "⇄", drawing: "✎" };

export function ObjectTree({
  panes,
  onVisible,
  onLock,
  onDelete,
  onRename,
  onSelect,
  onMove,
  onSettings,
  onDrop,
  onGroup,
  onGroupAction,
}: {
  panes: { title: string; pane: number; items: TreeItem[] }[];
  onVisible: (it: TreeItem) => void;
  onLock: (it: TreeItem) => void;
  onDelete: (it: TreeItem) => void;
  onRename: (it: TreeItem, name: string) => void;
  onSelect: (it: TreeItem) => void;
  onMove: (it: TreeItem, by: -1 | 1) => void;
  onSettings: (it: TreeItem) => void;
  /** Drag and drop: reorder drawings / indicators, move an indicator to another pane, put a drawing in a group. */
  onDrop?: (dragged: TreeItem, at: TreeDrop) => void;
  /** Group the picked drawings (Ctrl / Shift-click to pick several). */
  onGroup?: (ids: string[]) => void;
  onGroupAction?: (group: string, a: GroupAction) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [txt, setTxt] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [folded, setFolded] = useState<string[]>([]);
  const [drag, setDrag] = useState<TreeItem | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const allItems = panes.flatMap((p) => p.items);
  const pickedDrawings = picked.filter((id) => allItems.some((x) => x.kind === "drawing" && x.id === id));
  const drop = (at: TreeDrop) => (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setOver(null);
    if (drag && onDrop) onDrop(drag, at);
    setDrag(null);
  };
  const canDrop = (at: TreeDrop): boolean => {
    if (!drag) return false;
    if (at.kind === "group") return drag.kind === "drawing" && drag.group !== at.group;
    if (at.kind === "newpane") return drag.kind === "indicator";
    if (at.kind === "pane") return drag.kind === "indicator" || (drag.kind === "drawing" && !!drag.group);
    const t = at.item;
    if (t.id === drag.id) return false;
    return (drag.kind === "drawing" && t.kind === "drawing") || (drag.kind === "indicator" && t.kind === "indicator");
  };
  const dz = (key: string, at: TreeDrop) => ({
    onDragOver: (e: DragEvent) => {
      if (!canDrop(at)) return;
      e.preventDefault();
      if (over !== key) setOver(key);
    },
    onDragLeave: () => over === key && setOver(null),
    onDrop: drop(at),
  });
  const row = (it: TreeItem) => {
    const key = `${it.kind}:${it.id}`;
    const isPicked = picked.includes(it.id);
    return (
      <div
        key={key}
        className={`otree-row${it.selected || isPicked ? " on" : ""}${it.visible ? "" : " off"}${over === key ? " drop" : ""}${it.group ? " in-group" : ""}`}
        draggable={it.kind === "drawing" || it.kind === "indicator"}
        onDragStart={(e) => { setDrag(it); e.dataTransfer.effectAllowed = "move"; }}
        onDragEnd={() => { setDrag(null); setOver(null); }}
        {...dz(key, { kind: "item", item: it })}
        onClick={(e) => {
          if (it.kind === "drawing" && (e.ctrlKey || e.metaKey || e.shiftKey)) {
            setPicked((l) => (l.includes(it.id) ? l.filter((x) => x !== it.id) : [...l, it.id]));
            return;
          }
          setPicked(it.kind === "drawing" ? [it.id] : []);
          onSelect(it);
        }}
        data-testid={`otree-${it.kind}-${it.id}`}
      >
        <span className="otree-kind">{KIND_ICON[it.kind]}</span>
        {editing === key ? (
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
          <span className="otree-label" onDoubleClick={(e) => { if (it.kind !== "drawing") return; e.stopPropagation(); setTxt(it.label); setEditing(key); }} title={it.kind === "drawing" ? "Double-click to rename · drag to reorder or into a group · Ctrl-click to pick several" : it.kind === "indicator" ? "Drag to reorder, or onto another pane to move it" : undefined}>{it.label}</span>
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
    );
  };
  return (
    <div className="otree" data-testid="object-tree">
      {onGroup && (
        <div className="otree-bar">
          <button type="button" className="btn-sm" disabled={pickedDrawings.length < 2} onClick={() => { onGroup(pickedDrawings); setPicked([]); }} title="Ctrl / Shift-click drawings to pick several, then group them" data-testid="otree-group">
            ⊞ Group{pickedDrawings.length > 1 ? ` (${pickedDrawings.length})` : ""}
          </button>
          <span className="hint">{pickedDrawings.length > 1 ? `${pickedDrawings.length} picked` : "Ctrl-click to pick drawings"}</span>
        </div>
      )}
      {panes.map((p) => {
        // drawings of one group sit together under its folder, at the first member's place
        const seen = new Set<string>();
        const blocks: ({ group: string; items: TreeItem[] } | TreeItem)[] = [];
        for (const it of p.items) {
          if (!it.group) { blocks.push(it); continue; }
          if (seen.has(it.group)) continue;
          seen.add(it.group);
          blocks.push({ group: it.group, items: p.items.filter((x) => x.group === it.group) });
        }
        return (
          <div key={p.pane} className="otree-pane">
            <div className={`otree-ptitle${over === `pane:${p.pane}` ? " drop" : ""}`} {...dz(`pane:${p.pane}`, { kind: "pane", pane: p.pane })}>{p.title}</div>
            {p.items.length === 0 && <div className="hint">empty</div>}
            {blocks.map((b) => {
              if (!("group" in b && "items" in b && Array.isArray(b.items))) return row(b as TreeItem);
              const g = b as { group: string; items: TreeItem[] };
              const gkey = `group:${g.group}`;
              const hidden = g.items.every((x) => !x.visible);
              const locked = g.items.every((x) => x.locked);
              const isFolded = folded.includes(g.group);
              return (
                <div key={gkey} className="otree-group">
                  <div className={`otree-row otree-ghead${over === gkey ? " drop" : ""}${hidden ? " off" : ""}`} {...dz(gkey, { kind: "group", group: g.group })} onClick={() => setFolded((l) => (isFolded ? l.filter((x) => x !== g.group) : [...l, g.group]))} data-testid={`otree-group-${g.group}`}>
                    <span className="otree-kind">{isFolded ? "▸" : "▾"}</span>
                    {editing === gkey ? (
                      <input
                        className="chart-menu-input"
                        autoFocus
                        value={txt}
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => setTxt(e.target.value)}
                        onBlur={() => { if (txt.trim()) onGroupAction?.(g.group, { rename: txt.trim() }); setEditing(null); }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") { if (txt.trim()) onGroupAction?.(g.group, { rename: txt.trim() }); setEditing(null); }
                          if (e.key === "Escape") setEditing(null);
                        }}
                      />
                    ) : (
                      <span className="otree-label" onDoubleClick={(e) => { e.stopPropagation(); setTxt(g.group); setEditing(gkey); }} title="Double-click to rename">🗀 {g.group} <span className="hint">{g.items.length}</span></span>
                    )}
                    <span className="otree-acts" onClick={(e) => e.stopPropagation()}>
                      <button type="button" title={locked ? "Unlock group" : "Lock group"} onClick={() => onGroupAction?.(g.group, locked ? "unlock" : "lock")}>{locked ? "🔒" : "🔓"}</button>
                      <button type="button" title={hidden ? "Show group" : "Hide group"} onClick={() => onGroupAction?.(g.group, hidden ? "show" : "hide")}>{hidden ? "◌" : "👁"}</button>
                      <button type="button" title="Ungroup (keep the drawings)" onClick={() => onGroupAction?.(g.group, "ungroup")}>⊟</button>
                      <button type="button" title="Delete the group's drawings" onClick={() => onGroupAction?.(g.group, "delete")}>✕</button>
                    </span>
                  </div>
                  {!isFolded && <div className="otree-gitems">{g.items.map(row)}</div>}
                </div>
              );
            })}
          </div>
        );
      })}
      {onDrop && drag?.kind === "indicator" && (
        <div className={`otree-newpane${over === "newpane" ? " drop" : ""}`} {...dz("newpane", { kind: "newpane" })}>Drop here → new pane</div>
      )}
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
