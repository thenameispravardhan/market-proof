// chartUi — small building blocks for the chart's dialogs and menus:
// a modal (portaled into the fullscreen element when there is one, so it
// stays visible in fullscreen), tabs, a TradingView-style color picker,
// and compact form rows.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** Where overlays mount: the fullscreen element when present. */
export function overlayRoot(): HTMLElement {
  return (document.fullscreenElement as HTMLElement | null) ?? document.body;
}

/** Close on a mousedown outside `ref` (and on Escape). */
export function useOutside(ref: React.RefObject<HTMLElement | null>, active: boolean, onClose: () => void): void {
  const cb = useRef(onClose);
  cb.current = onClose;
  useEffect(() => {
    if (!active) return;
    const down = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) cb.current();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") cb.current();
    };
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", key);
    };
  }, [active, ref]);
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
      if (e.key === "Escape") {
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
  useOutside(ref, open, () => setOpen(false));
  const shown = value || fallback;
  return (
    <span className="cpick" ref={ref}>
      <button type="button" className="cpick-swatch" style={{ background: shown, opacity: opacity ?? 1 }} title={title} aria-label={title} onClick={() => setOpen((o) => !o)} />
      {open && (
        <div className="cpick-pop">
          {PALETTE.map((row, i) => (
            <div key={i} className="cpick-row">
              {row.map((c) => (
                <button key={c} type="button" className={`cpick-cell${c.toLowerCase() === shown.toLowerCase() ? " on" : ""}`} style={{ background: c }} title={c} onClick={() => onChange(c)} />
              ))}
            </div>
          ))}
          <div className="cpick-custom">
            <input type="color" value={/^#[0-9a-f]{6}$/i.test(shown) ? shown : "#2962ff"} onChange={(e) => onChange(e.target.value)} aria-label="Custom color" />
            {value && <button type="button" className="cpick-reset" onClick={() => onChange("")}>default</button>}
          </div>
          {onOpacity && (
            <label className="cpick-op">
              Opacity
              <input type="range" min={0} max={100} value={Math.round((opacity ?? 1) * 100)} onChange={(e) => onOpacity(Number(e.target.value) / 100)} />
              <span>{Math.round((opacity ?? 1) * 100)}%</span>
            </label>
          )}
        </div>
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

export function Num({ value, onChange, min, max, step = 1, width = 70, ariaLabel }: { value: number; onChange: (v: number) => void; min?: number; max?: number; step?: number; width?: number; ariaLabel?: string }) {
  const [txt, setTxt] = useState(String(value));
  useEffect(() => setTxt(String(value)), [value]);
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
      onChange={(e) => {
        setTxt(e.target.value);
        const v = Number(e.target.value);
        if (e.target.value !== "" && Number.isFinite(v)) onChange(Math.max(min ?? -Infinity, Math.min(max ?? Infinity, v)));
      }}
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
