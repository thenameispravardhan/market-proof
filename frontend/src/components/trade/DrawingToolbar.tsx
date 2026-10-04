// DrawingToolbar — the left drawing strip (TradingView-style groups with
// flyouts, last-used tool per group, favorite stars), the floating
// favorites bar, and the floating toolbar of a selected drawing.

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { EMOJIS, TOOLS, TOOL_BY_ID, TOOL_GROUPS, type Drawing, type DrawingStyle, type ToolGroupId } from "./drawings";
import { ColorInput, overlayRoot, useOutside } from "./chartUi";

export type CursorMode = "cross" | "dot" | "arrow" | "demo" | "eraser";
export type Magnet = "off" | "weak" | "strong";

export const CURSORS: { id: CursorMode; label: string; icon: string }[] = [
  { id: "cross", label: "Cross", icon: "✛" },
  { id: "dot", label: "Dot", icon: "•" },
  { id: "arrow", label: "Arrow", icon: "➚" },
  { id: "demo", label: "Demonstration", icon: "✺" },
  { id: "eraser", label: "Eraser", icon: "⌫" },
];

const DEFAULT_LAST: Record<ToolGroupId, string> = {
  lines: "trend",
  fib: "fib",
  patterns: "xabcd",
  forecast: "long",
  shapes: "brush",
  annotate: "text",
  icons: "icon",
};

/** A flyout next to its group button. Portaled with fixed positioning so
 *  the (scrolling) strip doesn't clip it. */
function Flyout({ children, onClose, wide, anchor }: { children: ReactNode; onClose: () => void; wide?: boolean; anchor: HTMLElement | null }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const cb = useRef(onClose);
  cb.current = onClose;
  useLayoutEffect(() => {
    if (!anchor) return;
    const r = anchor.getBoundingClientRect();
    const h = ref.current?.offsetHeight ?? 300;
    setPos({ left: r.right + 6, top: Math.max(8, Math.min(r.top, window.innerHeight - h - 8)) });
  }, [anchor]);
  useEffect(() => {
    const down = (e: MouseEvent) => {
      const t = e.target as Node;
      if (ref.current?.contains(t) || anchor?.contains(t)) return;
      cb.current();
    };
    const key = (e: KeyboardEvent) => e.key === "Escape" && cb.current();
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", key);
    };
  }, [anchor]);
  return createPortal(
    <div ref={ref} className={`dtb-fly${wide ? " wide" : ""}`} role="menu" style={pos ? { left: pos.left, top: pos.top } : { visibility: "hidden" }}>
      {children}
    </div>,
    overlayRoot(),
  );
}

export interface LeftToolbarProps {
  drawMode: string | null;
  cursor: CursorMode;
  onCursor: (c: CursorMode) => void;
  onTool: (id: string) => void;
  lastTool: Partial<Record<ToolGroupId, string>>;
  favorites: string[];
  onFav: (id: string) => void;
  magnet: Magnet;
  onMagnet: (m: Magnet) => void;
  stay: boolean;
  onStay: () => void;
  lockAll: boolean;
  onLockAll: () => void;
  hide: { drawings: boolean; indicators: boolean; positions: boolean };
  onHide: (k: "drawings" | "indicators" | "positions" | "all") => void;
  syncDrawings: boolean;
  onSyncDrawings: () => void;
  drawingCount: number;
  indicatorCount: number;
  onRemove: (k: "drawings" | "indicators" | "all") => void;
  selected: boolean;
  onDeleteSelected: () => void;
  showFavBar: boolean;
  onFavBar: () => void;
  onCollapse: () => void;
  onEmoji: (e: string) => void;
  onImage: (src: string, w: number, h: number) => void;
  alertMode: boolean;
  onAlert: () => void;
  pickMode?: boolean;
  onPick?: () => void;
}

export function LeftToolbar(p: LeftToolbarProps) {
  const [open, setOpen] = useState<string | null>(null);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [emojiQ, setEmojiQ] = useState("");
  const fileRef = useRef<HTMLInputElement | null>(null);
  const toggle = (id: string, e?: React.MouseEvent) => {
    if (e) setAnchor((e.currentTarget as HTMLElement).closest(".dtb-group") as HTMLElement | null);
    setOpen((o) => (o === id ? null : id));
  };
  const cursorDef = CURSORS.find((c) => c.id === p.cursor) ?? CURSORS[0];
  const groupBtn = (g: ToolGroupId) => {
    const last = p.lastTool[g] ?? DEFAULT_LAST[g];
    const tool = TOOL_BY_ID.get(last) ?? TOOLS.find((t) => t.group === g)!;
    const activeInGroup = p.drawMode != null && TOOL_BY_ID.get(p.drawMode)?.group === g && p.drawMode !== "measure";
    const label = TOOL_GROUPS.find((x) => x.id === g)?.label ?? g;
    const tools = TOOLS.filter((t) => t.group === g && t.id !== "measure");
    const sections = [...new Set(tools.map((t) => t.section ?? ""))];
    return (
      <div key={g} className="dtb-group">
        <button
          type="button"
          className={`chart-tool${activeInGroup ? " on" : ""}`}
          onClick={(e) => (g === "icons" ? toggle(g, e) : p.onTool(tool.id))}
          title={g === "icons" ? "Icons" : `${tool.label}`}
          data-testid={`chart-draw-${g === "icons" ? "icons" : tool.id}`}
        >
          {g === "icons" ? "☺" : tool.icon}
        </button>
        {g !== "icons" && <button type="button" className="dtb-more" onClick={(e) => toggle(g, e)} title={label} aria-label={label} data-testid={`chart-group-${g}`}>›</button>}
        {open === g && g !== "icons" && (
          <Flyout anchor={anchor} onClose={() => setOpen(null)}>
            <div className="dtb-fly-title">{label}</div>
            {sections.map((sec) => (
              <div key={sec}>
                {sec && <div className="dtb-fly-sec">{sec}</div>}
                {tools.filter((t) => (t.section ?? "") === sec).map((t) => (
                  <div key={t.id} className={`dtb-fly-row${p.drawMode === t.id ? " on" : ""}`}>
                    <button type="button" className="dtb-fly-item" onClick={() => { p.onTool(t.id); setOpen(null); }} data-testid={`chart-draw-${t.id}`}>
                      <span className="ico">{t.icon}</span>
                      {t.label}
                    </button>
                    <button type="button" className={`dtb-star${p.favorites.includes(t.id) ? " on" : ""}`} onClick={() => p.onFav(t.id)} title="Add to favorites" aria-label={`Favorite ${t.label}`}>★</button>
                  </div>
                ))}
              </div>
            ))}
          </Flyout>
        )}
        {open === "icons" && g === "icons" && (
          <Flyout anchor={anchor} onClose={() => setOpen(null)} wide>
            <input className="chart-menu-input" placeholder="Search icons" value={emojiQ} onChange={(e) => setEmojiQ(e.target.value)} autoFocus aria-label="Search icons" />
            {[...new Set(EMOJIS.map((e) => e.cat))].map((cat) => {
              const list = EMOJIS.filter((e) => e.cat === cat && (!emojiQ || e.k.includes(emojiQ.toLowerCase())));
              if (!list.length) return null;
              return (
                <div key={cat}>
                  <div className="dtb-fly-sec">{cat}</div>
                  <div className="dtb-emoji">
                    {list.map((e) => (
                      <button key={e.e} type="button" title={e.k} onClick={() => { p.onEmoji(e.e); setOpen(null); }}>{e.e}</button>
                    ))}
                  </div>
                </div>
              );
            })}
          </Flyout>
        )}
      </div>
    );
  };
  return (
    <div className="chart-tools" role="toolbar" aria-label="drawing tools">
      <div className="dtb-group">
        <button
          type="button"
          className={`chart-tool${!p.drawMode ? " on" : ""}`}
          onClick={() => p.onCursor(p.cursor)}
          title={`${cursorDef.label} (Esc)`}
          data-testid="chart-draw-none"
        >
          {cursorDef.icon}
        </button>
        <button type="button" className="dtb-more" onClick={(e) => toggle("cursor", e)} title="Cursors" aria-label="Cursors">›</button>
        {open === "cursor" && (
          <Flyout anchor={anchor} onClose={() => setOpen(null)}>
            <div className="dtb-fly-title">Cursors</div>
            {CURSORS.map((c) => (
              <div key={c.id} className={`dtb-fly-row${p.cursor === c.id ? " on" : ""}`}>
                <button type="button" className="dtb-fly-item" onClick={() => { p.onCursor(c.id); setOpen(null); }} data-testid={`chart-cursor-${c.id}`}>
                  <span className="ico">{c.icon}</span>
                  {c.label}
                </button>
              </div>
            ))}
          </Flyout>
        )}
      </div>
      {TOOL_GROUPS.map((g) => groupBtn(g.id))}
      <div className="dtb-group">
        <button type="button" className={`chart-tool${p.drawMode === "image" ? " on" : ""}`} title="Image" onClick={() => fileRef.current?.click()} data-testid="chart-draw-image">▣</button>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (!f) return;
            if (f.size > 400 * 1024) {
              window.alert("Images up to 400 KB (drawings are saved in this browser).");
              return;
            }
            const r = new FileReader();
            r.onload = () => {
              const src = String(r.result);
              const img = new Image();
              img.onload = () => {
                const k = Math.min(1, 240 / Math.max(img.width, img.height));
                p.onImage(src, Math.round(img.width * k), Math.round(img.height * k));
              };
              img.onerror = () => p.onImage(src, 160, 100);
              img.src = src;
            };
            r.readAsDataURL(f);
          }}
        />
      </div>
      <div className="chart-tool-sep" />
      <button type="button" className={`chart-tool${p.drawMode === "measure" ? " on" : ""}`} onClick={() => p.onTool("measure")} title="Measure — click start, click end" data-testid="chart-measure">📏</button>
      <button type="button" className={`chart-tool${p.drawMode === "zoom" ? " on" : ""}`} onClick={() => p.onTool("zoom")} title="Zoom in — click two corners of the area" data-testid="chart-zoom">🔍</button>
      <div className="dtb-group">
        <button
          type="button"
          className={`chart-tool${p.magnet !== "off" ? " on" : ""}`}
          onClick={() => p.onMagnet(p.magnet === "off" ? "weak" : "off")}
          title={`Magnet mode${p.magnet === "off" ? "" : ` (${p.magnet})`} — snaps drawings placed near price bars to the closest OHLC value`}
          data-testid="chart-magnet"
        >
          🧲
        </button>
        <button type="button" className="dtb-more" onClick={(e) => toggle("magnet", e)} title="Magnet options" aria-label="Magnet options">›</button>
        {open === "magnet" && (
          <Flyout anchor={anchor} onClose={() => setOpen(null)}>
            {(["weak", "strong", "off"] as Magnet[]).map((m) => (
              <div key={m} className={`dtb-fly-row${p.magnet === m ? " on" : ""}`}>
                <button type="button" className="dtb-fly-item" onClick={() => { p.onMagnet(m); setOpen(null); }}>
                  {m === "weak" ? "Weak magnet" : m === "strong" ? "Strong magnet" : "Magnet off"}
                </button>
              </div>
            ))}
          </Flyout>
        )}
      </div>
      <button type="button" className={`chart-tool${p.stay ? " on" : ""}`} onClick={p.onStay} title="Stay in drawing mode" data-testid="chart-stay">✎̲</button>
      <button type="button" className={`chart-tool${p.lockAll ? " on" : ""}`} onClick={p.onLockAll} title={p.lockAll ? "Unlock all drawings" : "Lock all drawings"} data-testid="chart-lock-all">{p.lockAll ? "🔒" : "🔓"}</button>
      <div className="dtb-group">
        <button
          type="button"
          className={`chart-tool${p.hide.drawings ? " on" : ""}`}
          onClick={() => p.onHide("drawings")}
          title={p.hide.drawings ? "Show all drawings" : "Hide all drawings"}
          data-testid="chart-hide-drawings"
        >
          {p.hide.drawings ? "◌" : "👁"}
        </button>
        <button type="button" className="dtb-more" onClick={(e) => toggle("hide", e)} title="Hide options" aria-label="Hide options">›</button>
        {open === "hide" && (
          <Flyout anchor={anchor} onClose={() => setOpen(null)}>
            {([["drawings", "Hide drawings"], ["indicators", "Hide indicators"], ["positions", "Hide positions & orders"], ["all", "Hide all"]] as const).map(([k, l]) => (
              <div key={k} className={`dtb-fly-row${k !== "all" && p.hide[k] ? " on" : ""}`}>
                <button type="button" className="dtb-fly-item" onClick={() => { p.onHide(k); setOpen(null); }}>{k !== "all" && p.hide[k] ? "✓ " : ""}{l}</button>
              </div>
            ))}
          </Flyout>
        )}
      </div>
      <button type="button" className={`chart-tool${p.syncDrawings ? " on" : ""}`} onClick={p.onSyncDrawings} title="Sync drawings to all charts of this symbol" data-testid="chart-sync-drawings">🔗</button>
      <div className="dtb-group">
        <button type="button" className="chart-tool" disabled={!p.selected} onClick={p.onDeleteSelected} title="Delete selected drawing (Del)" data-testid="chart-draw-delete">⌫</button>
      </div>
      <div className="dtb-group">
        <button type="button" className="chart-tool" onClick={(e) => toggle("remove", e)} title="Remove drawings / indicators" data-testid="chart-draw-clear">🗑</button>
        {open === "remove" && (
          <Flyout anchor={anchor} onClose={() => setOpen(null)}>
            <div className="dtb-fly-row"><button type="button" className="dtb-fly-item" disabled={!p.drawingCount} onClick={() => { p.onRemove("drawings"); setOpen(null); }} data-testid="chart-remove-drawings">Remove {p.drawingCount} drawing{p.drawingCount === 1 ? "" : "s"}</button></div>
            <div className="dtb-fly-row"><button type="button" className="dtb-fly-item" disabled={!p.indicatorCount} onClick={() => { p.onRemove("indicators"); setOpen(null); }}>Remove {p.indicatorCount} indicator{p.indicatorCount === 1 ? "" : "s"}</button></div>
            <div className="dtb-fly-row"><button type="button" className="dtb-fly-item" onClick={() => { p.onRemove("all"); setOpen(null); }}>Remove drawings & indicators</button></div>
          </Flyout>
        )}
      </div>
      <div className="chart-tool-sep" />
      <button type="button" className={`chart-tool${p.alertMode ? " on" : ""}`} onClick={p.onAlert} title="Price alert — click a price" data-testid="chart-draw-alert">🔔</button>
      {p.onPick && (
        <button type="button" className={`chart-tool${p.pickMode ? " on" : ""}`} onClick={p.onPick} title="Click a chart price to load it into the ticket as a LIMIT" data-testid="chart-pick-price">⤷</button>
      )}
      <button type="button" className={`chart-tool${p.showFavBar ? " on" : ""}`} onClick={p.onFavBar} title="Show favorite drawing tools toolbar" data-testid="chart-fav-bar">★</button>
      <span className="grow" />
      <button type="button" className="chart-tool" onClick={p.onCollapse} title="Hide drawings toolbar" data-testid="chart-tools-collapse">«</button>
    </div>
  );
}

/** Floating, draggable bar of the favorite tools. */
export function FavoritesBar({ favorites, drawMode, onTool, onClose }: { favorites: string[]; drawMode: string | null; onTool: (id: string) => void; onClose: () => void }) {
  const [pos, setPos] = useState(() => {
    try { return JSON.parse(localStorage.getItem("chart:favBarPos") ?? "null") ?? { x: 60, y: 8 }; } catch { return { x: 60, y: 8 }; }
  });
  const drag = (e: React.PointerEvent) => {
    e.preventDefault();
    const sx = e.clientX - pos.x, sy = e.clientY - pos.y;
    let last = pos;
    const move = (ev: PointerEvent) => { last = { x: Math.max(0, ev.clientX - sx), y: Math.max(0, ev.clientY - sy) }; setPos(last); };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      try { localStorage.setItem("chart:favBarPos", JSON.stringify(last)); } catch { /* best-effort */ }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  return (
    <div className="fav-bar" style={{ left: pos.x, top: pos.y }} data-testid="chart-fav-toolbar">
      <span className="fav-grip" onPointerDown={drag} title="Drag">⋮⋮</span>
      {favorites.length === 0 && <span className="hint">star tools in the flyouts</span>}
      {favorites.map((id) => {
        const t = TOOL_BY_ID.get(id);
        if (!t) return null;
        return <button key={id} type="button" className={`chart-tool${drawMode === id ? " on" : ""}`} title={t.label} onClick={() => onTool(id)}>{t.icon}</button>;
      })}
      <button type="button" className="chart-tool" onClick={onClose} title="Close">×</button>
    </div>
  );
}

const WIDTHS = [1, 2, 3, 4];

/** The floating toolbar over a selected drawing. */
export function DrawingFloatBar({
  drawing,
  style,
  fallbackColor,
  onStyle,
  onText,
  onSettings,
  onLock,
  onHide,
  onClone,
  onDelete,
  onOrder,
  onSaveDefault,
  onAlert,
  canAlert,
}: {
  drawing: Drawing;
  style: DrawingStyle;
  fallbackColor: string;
  onStyle: (s: Partial<DrawingStyle>) => void;
  onText?: () => void;
  onSettings: () => void;
  onLock: () => void;
  onHide: () => void;
  onClone: () => void;
  onDelete: () => void;
  onOrder: (k: "front" | "forward" | "backward" | "back") => void;
  onSaveDefault: () => void;
  onAlert: () => void;
  canAlert: boolean;
}) {
  const [more, setMore] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useOutside(ref, more, () => setMore(false));
  const tool = TOOL_BY_ID.get(drawing.type);
  return (
    <div className="dfloat" data-testid="drawing-floatbar" onMouseDown={(e) => e.stopPropagation()}>
      <span className="dfloat-name">{drawing.name || tool?.label}</span>
      <ColorInput value={style.color} fallback={fallbackColor} onChange={(c) => onStyle({ color: c })} title="Line color" />
      <select className="cform-sel" value={style.width} onChange={(e) => onStyle({ width: Number(e.target.value) })} aria-label="Line width">
        {WIDTHS.map((w) => <option key={w} value={w}>{w}px</option>)}
      </select>
      <select className="cform-sel" value={style.dash} onChange={(e) => onStyle({ dash: Number(e.target.value) as 0 | 1 | 2 })} aria-label="Line style">
        <option value={0}>──</option>
        <option value={2}>- -</option>
        <option value={1}>···</option>
      </select>
      {onText && <button type="button" className="chart-tool" onClick={onText} title="Edit text">T</button>}
      <button type="button" className="chart-tool" onClick={onSettings} title="Settings" data-testid="drawing-settings-btn">⚙</button>
      {canAlert && <button type="button" className="chart-tool" onClick={onAlert} title="Add alert on this line">🔔</button>}
      <button type="button" className={`chart-tool${drawing.locked ? " on" : ""}`} onClick={onLock} title={drawing.locked ? "Unlock" : "Lock"}>{drawing.locked ? "🔒" : "🔓"}</button>
      <button type="button" className="chart-tool" onClick={onDelete} title="Remove" data-testid="drawing-delete">🗑</button>
      <div className="chart-menu-wrap" ref={ref}>
        <button type="button" className="chart-tool" onClick={() => setMore((m) => !m)} title="More">⋯</button>
        {more && (
          <div className="chart-menu cdrop right">
            <button type="button" className="chart-menu-item" onClick={() => { onClone(); setMore(false); }}>Clone<span className="kbd">Ctrl+drag</span></button>
            <button type="button" className="chart-menu-item" onClick={() => { onHide(); setMore(false); }}>Hide</button>
            <div className="chart-menu-sep" />
            <button type="button" className="chart-menu-item" onClick={() => { onOrder("front"); setMore(false); }}>Bring to front</button>
            <button type="button" className="chart-menu-item" onClick={() => { onOrder("forward"); setMore(false); }}>Bring forward</button>
            <button type="button" className="chart-menu-item" onClick={() => { onOrder("backward"); setMore(false); }}>Send backward</button>
            <button type="button" className="chart-menu-item" onClick={() => { onOrder("back"); setMore(false); }}>Send to back</button>
            <div className="chart-menu-sep" />
            <button type="button" className="chart-menu-item" onClick={() => { onSaveDefault(); setMore(false); }}>Save style as default for {tool?.label ?? "tool"}</button>
          </div>
        )}
      </div>
    </div>
  );
}
