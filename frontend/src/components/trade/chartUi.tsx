// chartUi — small building blocks for the chart's dialogs and menus:
// a modal (portaled into the fullscreen element when there is one, so it
// stays visible in fullscreen), tabs, a TradingView-style color picker,
// and compact form rows.

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** Where overlays mount: the fullscreen element when present. */
export function overlayRoot(): HTMLElement {
  return (document.fullscreenElement as HTMLElement | null) ?? document.body;
}

/** Popups (menus, color pickers) open right now. While one is open,
 *  Escape closes it instead of the dialog around it. */
let openPopups = 0;

/** Close on a mousedown outside `ref` (and on Escape). `alsoInside` is a
 *  second element that counts as inside (a popup portaled elsewhere). */
export function useOutside(
  ref: React.RefObject<HTMLElement | null>,
  active: boolean,
  onClose: () => void,
  alsoInside?: React.RefObject<HTMLElement | null>,
): void {
  const cb = useRef(onClose);
  cb.current = onClose;
  useEffect(() => {
    if (!active) return;
    openPopups += 1;
    const down = (e: MouseEvent) => {
      const t = e.target as Node;
      if (alsoInside?.current?.contains(t)) return;
      if (ref.current && !ref.current.contains(t)) cb.current();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") cb.current();
    };
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => {
      openPopups -= 1;
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", key);
    };
  }, [active, ref, alsoInside]);
}

export function Modal({
  title,
  onClose,
  children,
  footer,
  width = 520,
  tabs,
  tab,
  onTab,
  testid,
  className = "",
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
  tabs?: { id: string; label: string }[];
  tab?: string;
  onTab?: (id: string) => void;
  testid?: string;
  className?: string;
}) {
  const cb = useRef(onClose);
  cb.current = onClose;
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      // a popup inside the dialog takes the first Escape
      if (e.key === "Escape" && openPopups === 0) {
        e.stopPropagation();
        cb.current();
      }
    };
    window.addEventListener("keydown", key, true);
    return () => window.removeEventListener("keydown", key, true);
  }, []);
  return createPortal(
    <div className="cmodal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`cmodal ${className}`} style={{ width }} role="dialog" aria-label={typeof title === "string" ? title : undefined} data-testid={testid}>
        <header className="cmodal-head">
          <span className="cmodal-title">{title}</span>
          <button type="button" className="cmodal-x" onClick={onClose} aria-label="Close">×</button>
        </header>
        {tabs ? (
          <div className="cmodal-split">
            <nav className="cmodal-tabs" role="tablist">
              {tabs.map((t) => (
                <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} className={tab === t.id ? "on" : ""} onClick={() => onTab?.(t.id)}>
                  {t.label}
                </button>
              ))}
            </nav>
            <div className="cmodal-body">{children}</div>
          </div>
        ) : (
          <div className="cmodal-body">{children}</div>
        )}
        {footer && <footer className="cmodal-foot">{footer}</footer>}
      </div>
    </div>,
    overlayRoot(),
  );
}

export const PALETTE = [
  ["#FFFFFF", "#D1D4DC", "#B2B5BE", "#9598A1", "#787B86", "#5D606B", "#434651", "#2A2E39", "#131722", "#000000"],
  ["#F23645", "#FF9800", "#FFEB3B", "#4CAF50", "#089981", "#00BCD4", "#2962FF", "#673AB7", "#9C27B0", "#E91E63"],
  ["#FCCBCD", "#FFE0B2", "#FFF9C4", "#C8E6C9", "#ACE5DC", "#B2EBF2", "#BBD9FB", "#D1C4E9", "#E1BEE7", "#F8BBD0"],
  ["#801922", "#E65100", "#F57F17", "#1B5E20", "#004D40", "#006064", "#0C3299", "#311B92", "#4A148C", "#880E4F"],
];

export function ColorInput({
  value,
  fallback,
  onChange,
  opacity,
  onOpacity,
  title = "Color",
}: {
  value: string;
  fallback: string;
  onChange: (c: string) => void;
  opacity?: number;
  onOpacity?: (a: number) => void;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  useOutside(ref, open, () => setOpen(false), popRef);
  const shown = value || fallback;
  // The popup is portaled and fixed-positioned so a scrolling dialog body
  // can't clip it; it opens below the swatch, or above when there's no room.
  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    const place = () => {
      const sw = ref.current?.getBoundingClientRect();
      const pop = popRef.current;
      if (!sw) return;
      const h = pop?.offsetHeight ?? 0;
      const w = pop?.offsetWidth ?? 0;
      const below = sw.bottom + 4;
      const top = below + h > window.innerHeight - 8 && sw.top - h - 4 >= 8 ? sw.top - h - 4 : below;
      const left = Math.max(8, Math.min(sw.left, window.innerWidth - w - 8));
      setPos((p) => (p && p.top === top && p.left === left ? p : { top, left }));
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, !!onOpacity]);
  return (
    <span className="cpick" ref={ref}>
      <button type="button" className="cpick-swatch" style={{ background: shown, opacity: opacity ?? 1 }} title={title} aria-label={title} aria-expanded={open} onClick={() => setOpen((o) => !o)} />
      {open &&
        createPortal(
          <div className="cpick-pop" ref={popRef} style={{ position: "fixed", top: pos?.top ?? -9999, left: pos?.left ?? -9999 }} data-testid="color-popup">
            {PALETTE.map((row, i) => (
              <div key={i} className="cpick-row">
                {row.map((c) => (
                  <button key={c} type="button" className={`cpick-cell${c.toLowerCase() === shown.toLowerCase() ? " on" : ""}`} style={{ background: c }} title={c} aria-label={c} onClick={() => onChange(c)} />
                ))}
              </div>
            ))}
            <div className="cpick-custom">
              <input type="color" value={/^#[0-9a-f]{6}$/i.test(shown) ? shown : "#2962ff"} onChange={(e) => onChange(e.target.value)} aria-label="Custom color" />
              {value && value.toLowerCase() !== fallback.toLowerCase() && <button type="button" className="cpick-reset" onClick={() => onChange("")}>Reset to default</button>}
            </div>
            {onOpacity && (
              <label className="cpick-op">
                Opacity
                <input type="range" min={0} max={100} value={Math.round((opacity ?? 1) * 100)} onChange={(e) => onOpacity(Number(e.target.value) / 100)} />
                <span>{Math.round((opacity ?? 1) * 100)}%</span>
              </label>
            )}
          </div>,
          overlayRoot(),
        )}
    </span>
  );
}

export function Row({ label, children, hint }: { label: ReactNode; children?: ReactNode; hint?: string }) {
  return (
    <div className="cform-row" title={hint}>
      <span className="cform-label">{label}</span>
      <span className="cform-ctl">{children}</span>
    </div>
  );
}

export function Check({ label, checked, onChange, children, testid }: { label: ReactNode; checked: boolean; onChange: (v: boolean) => void; children?: ReactNode; testid?: string }) {
  return (
    <div className="cform-row">
      <label className="cform-check">
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} data-testid={testid} />
        {label}
      </label>
      {children && <span className="cform-ctl">{children}</span>}
    </div>
  );
}

export function Sel<T extends string | number>({ value, options, onChange, ariaLabel }: { value: T; options: { v: T; l: string }[]; onChange: (v: T) => void; ariaLabel?: string }) {
  return (
    <select className="cform-sel" value={String(value)} aria-label={ariaLabel} onChange={(e) => {
      const hit = options.find((o) => String(o.v) === e.target.value);
      if (hit) onChange(hit.v);
    }}>
      {options.map((o) => <option key={String(o.v)} value={String(o.v)}>{o.l}</option>)}
    </select>
  );
}

export function Num({ value, onChange, min, max, step = 1, width = 70, ariaLabel, disabled }: { value: number; onChange: (v: number) => void; min?: number; max?: number; step?: number; width?: number; ariaLabel?: string; disabled?: boolean }) {
  const [txt, setTxt] = useState(String(value));
  useEffect(() => setTxt(String(value)), [value]);
  const lo = min ?? -Infinity;
  const hi = max ?? Infinity;
  return (
    <input
      className="cform-num"
      type="number"
      style={{ width }}
      value={txt}
      min={min}
      max={max}
      step={step}
      aria-label={ariaLabel}
      disabled={disabled}
      onChange={(e) => {
        setTxt(e.target.value);
        // only in-range values apply while typing, so "1" on the way to
        // "14" (min 2) isn't snapped to 2 under the cursor
        const v = Number(e.target.value);
        if (e.target.value !== "" && Number.isFinite(v) && v >= lo && v <= hi) onChange(v);
      }}
      onBlur={() => {
        // leaving the field: clamp what was typed, or restore a blank
        const v = Number(txt);
        if (txt.trim() === "" || !Number.isFinite(v)) {
          setTxt(String(value));
          return;
        }
        const c = Math.max(lo, Math.min(hi, v));
        if (c !== value) onChange(c);
        setTxt(String(c));
      }}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="cform-sec">
      <div className="cform-sec-title">{title}</div>
      {children}
    </div>
  );
}

/** A simple dropdown menu anchored under a button. */
export function Dropdown({
  button,
  children,
  className = "",
  align = "left",
  open: openProp,
  onOpen,
}: {
  button: (open: boolean, toggle: () => void) => ReactNode;
  children: (close: () => void) => ReactNode;
  className?: string;
  align?: "left" | "right";
  open?: boolean;
  onOpen?: (o: boolean) => void;
}) {
  const [own, setOwn] = useState(false);
  const open = openProp ?? own;
  const set = (o: boolean) => (onOpen ? onOpen(o) : setOwn(o));
  const ref = useRef<HTMLDivElement | null>(null);
  useOutside(ref, open, () => set(false));
  return (
    <div className={`chart-menu-wrap ${className}`} ref={ref}>
      {button(open, () => set(!open))}
      {open && <div className={`chart-menu cdrop ${align === "right" ? "right" : ""}`}>{children(() => set(false))}</div>}
    </div>
  );
}
