// ChartPopout — Tools → Popout Chart: one chart, its own browser window,
// bound to the symbol and interval it was opened with (#/trade?popout=1&…).
// The window still runs the app shell's WebSocket, so it ticks live; it has
// no ticket, panels or account manager — just the chart and its toolbars.

import { useEffect, useState } from "react";
import type { InstrumentHit } from "../../types";
import ChartPanel from "./ChartPanel";

export function popoutParams(): { symbol: string; name: string; iv: string | null } | null {
  const q = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
  if (q.get("popout") !== "1") return null;
  const symbol = (q.get("symbol") ?? "").trim();
  return symbol ? { symbol, name: q.get("name") ?? symbol.split(":").pop() ?? symbol, iv: q.get("iv") } : null;
}

export default function ChartPopout() {
  const p = popoutParams();
  const [hit, setHit] = useState<InstrumentHit | null>(() =>
    p ? ({ symbol: p.symbol, short_name: p.name, exchange: p.symbol.split(":")[0], segment: "", instrument_type: "", lot_size: 1, tick_size: 0.05, expiry: null, strike: null, underlying: null, display: p.name } as InstrumentHit) : null,
  );
  // the opener's interval wins once, then the chart remembers its own
  useEffect(() => {
    if (!p?.iv) return;
    try {
      const prefs = JSON.parse(localStorage.getItem("chart:prefs") ?? "{}");
      localStorage.setItem("chart:prefs", JSON.stringify({ ...prefs, interval: p.iv }));
    } catch {
      /* best-effort */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (hit) document.title = `${hit.short_name} · chart`;
  }, [hit]);
  if (!hit) return <div className="hint" style={{ padding: 20 }}>No symbol to chart.</div>;
  return (
    <div className="popout-chart" data-testid="popout-chart">
      <ChartPanel
        key={hit.symbol}
        symbol={hit.symbol}
        shortName={hit.short_name}
        instrument={hit}
        onSymbolChange={(h) => {
          setHit(h);
          const q = new URLSearchParams({ popout: "1", symbol: h.symbol, name: h.short_name });
          window.history.replaceState(null, "", `#/trade?${q.toString()}`);
        }}
      />
    </div>
  );
}
