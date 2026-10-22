// Shared number box for the Algo Lab editors.

import { useEffect, useRef, useState } from "react";

// A number box that lets you TYPE decimals ("0.", "1.05") — a controlled
// <input type=number> fights partial input in some browsers. Keeps the raw
// text locally and reports only complete numbers.
export function NumInput({ value, onChange, width = 64, allowEmpty = false, signed = false, title, placeholder }: {
  value: number | null | undefined; onChange: (v: number | null) => void; width?: number;
  allowEmpty?: boolean; signed?: boolean; title?: string; placeholder?: string;
}) {
  const shown = value === null || value === undefined ? "" : String(value);
  const [text, setText] = useState(shown);
  const committed = useRef(value);
  useEffect(() => {
    if (value !== committed.current) { committed.current = value; setText(shown); }
  }, [value, shown]);
  const re = signed ? /^-?\d*\.?\d*$/ : /^\d*\.?\d*$/;
  return (
    <input type="text" inputMode="decimal" style={{ width }} value={text} title={title} placeholder={placeholder}
      onChange={(e) => {
        const t = e.target.value.trim();
        if (!re.test(t)) return;
        setText(t);
        if (t === "") { if (allowEmpty) { committed.current = null; onChange(null); } return; }
        if (t === "-" || t === "." || t.endsWith(".")) return;
        const n = Number(t);
        if (!Number.isNaN(n)) { committed.current = n; onChange(n); }
      }}
      onBlur={() => { if (text === "" && !allowEmpty) setText(shown); else if (text.endsWith(".")) setText(text.slice(0, -1)); }} />
  );
}
