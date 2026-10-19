/** Working orders on the chart, TradingView-style: each one a dashed line with
 *  a tag you drag to move the order. Paper orders also carry the SL / target
 *  they arm on fill — pull one out of the tag (SL / TP), drag it, ✕ clears it. */
import { useEffect, useRef, useState } from "react";
import { LEVEL_DRAG_SLOP, snapToTick } from "./levelDrag";

export interface ChartWorkingOrder {
  id: string;
  side: "BUY" | "SELL";
  qty: number;
  /** LIMIT | SL-M | STOP_LOSS … (only LIMIT and SL-M are draggable). */
  type: string;
  price: number;
  sl?: number | null;
  tp?: number | null;
  /** This order can arm an SL / target (paper orders). */
  levels: boolean;
}

export interface OrderPatch { price?: number; sl?: number | null; tp?: number | null }

type Which = "price" | "sl" | "tp";

export interface OrderGeo {
  priceToY: (p: number) => number | null;
  yToPrice: (y: number) => number | null;
  height: number;
  left: number;
  right: number;
  top: number;   // the chart host's client top
}

export default function OrderLayer({ orders, geo, tick, fmt, onModify, onCancel, toast }: {
  orders: ChartWorkingOrder[];
  geo: () => OrderGeo | null;
  tick: number;
  fmt: (v: number) => string;
  onModify: (o: ChartWorkingOrder, patch: OrderPatch) => Promise<void>;
  onCancel?: (o: ChartWorkingOrder) => void;
  toast: (msg: string) => void;
}) {
  const rows = useRef(new Map<string, HTMLDivElement>());
  const drag = useRef<{ id: string; which: Which; price: number } | null>(null);
  const [dragKey, setDragKey] = useState<string | null>(null);
  const geoRef = useRef(geo);
  geoRef.current = geo;

  const priceOf = (o: ChartWorkingOrder, w: Which): number | null | undefined => {
    const d = drag.current;
    if (d && d.id === o.id && d.which === w) return d.price;
    return w === "price" ? o.price : o[w];
  };

  // Glue every line + tag to its price each frame (pans, zooms, ticks, drags).
  useEffect(() => {
    let raf = 0;
    const frame = () => {
      raf = requestAnimationFrame(frame);
      const g = geoRef.current();
      if (!g) return;
      for (const o of orders) {
        for (const w of ["price", "sl", "tp"] as const) {
          const el = rows.current.get(`${o.id}:${w}`);
          const p = priceOf(o, w);
          if (!el) continue;
          const y = p == null ? null : g.priceToY(p);
          if (y == null) { el.style.visibility = "hidden"; continue; }
          el.style.visibility = "visible";
          el.style.left = `${g.left}px`;
          el.style.right = `${g.right}px`;
          el.style.transform = `translateY(${Math.max(0, Math.min(g.height, y))}px)`;
          el.classList.toggle("off", y < 0 || y > g.height);
          const lbl = el.querySelector<HTMLElement>(".px");
          if (lbl && p != null) lbl.textContent = fmt(p);
        }
      }
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orders]);

  function start(e: React.PointerEvent, o: ChartWorkingOrder, which: Which): void {
    if (e.button !== 0) return;
    if (which === "price" && o.type !== "LIMIT" && o.type !== "SL-M") {
      toast("Only limit and SL-M orders can be dragged — modify this one in the Orders tab");
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    const startY = e.clientY;
    let moved = false;
    drag.current = { id: o.id, which, price: priceOf(o, which) ?? o.price };
    setDragKey(`${o.id}:${which}`);
    const move = (ev: PointerEvent) => {
      if (!moved && Math.abs(ev.clientY - startY) < LEVEL_DRAG_SLOP) return;
      moved = true;
      const g = geoRef.current();
      const pr = g ? g.yToPrice(ev.clientY - g.top) : null;
      if (pr != null && drag.current) drag.current.price = snapToTick(pr, tick);
    };
    const finish = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", finish);
      drag.current = null;
      setDragKey(null);
    };
    const up = async () => {
      const d = drag.current;
      window.removeEventListener("pointermove", move);
      if (!d || !moved || d.price === (which === "price" ? o.price : o[which])) return finish();
      // A level on the wrong side of the order would exit the moment it fills.
      const entry = which === "price" ? d.price : o.price;
      const long = o.side === "BUY";
      const sl = which === "sl" ? d.price : o.sl;
      const tp = which === "tp" ? d.price : o.tp;
      if (sl != null && (long ? sl >= entry : sl <= entry)) { toast(`Stop-loss must be ${long ? "below" : "above"} the order price ${fmt(entry)} — not changed`); return finish(); }
      if (tp != null && (long ? tp <= entry : tp >= entry)) { toast(`Target must be ${long ? "above" : "below"} the order price ${fmt(entry)} — not changed`); return finish(); }
      try {
        await onModify(o, which === "price" ? { price: d.price } : { [which]: d.price });
        toast(`${which === "price" ? `${o.side} ${o.qty}` : which === "sl" ? "Stop-loss" : "Target"} → ${fmt(d.price)}`);
      } catch (err) {
        toast(`Not changed: ${err instanceof Error ? err.message : String(err)}`);
      }
      finish();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", finish);
  }

  const clear = (o: ChartWorkingOrder, which: "sl" | "tp") =>
    void onModify(o, { [which]: null }).then(() => toast(`${which === "sl" ? "Stop-loss" : "Target"} removed`), (err) => toast(`Not changed: ${err instanceof Error ? err.message : String(err)}`));

  const ref = (k: string) => (el: HTMLDivElement | null) => { if (el) rows.current.set(k, el); else rows.current.delete(k); };
  return (
    <div className="ord-layer">
      {orders.map((o) => {
        const buy = o.side === "BUY";
        return (
          <div key={o.id}>
            <div ref={ref(`${o.id}:price`)} className={`ord-row ${buy ? "buy" : "sell"}${dragKey === `${o.id}:price` ? " dragging" : ""}`} data-testid={`chart-order-${o.id}`}>
              <div className="pos-tag ord" onPointerDown={(e) => start(e, o, "price")} title="Drag to move the order">
                <span className="side">{o.side} {o.qty} {o.type === "LIMIT" ? "LMT" : o.type}</span>
                <span className="px" />
                {o.levels && o.tp == null && <span className="grab tp" style={dragKey === `${o.id}:tp` ? { visibility: "hidden" } : undefined} onPointerDown={(e) => start(e, o, "tp")} title="Drag to set the target this order arms on fill">TP</span>}
                {o.levels && o.sl == null && <span className="grab sl" style={dragKey === `${o.id}:sl` ? { visibility: "hidden" } : undefined} onPointerDown={(e) => start(e, o, "sl")} title="Drag to set the stop-loss this order arms on fill">SL</span>}
                {onCancel && <button type="button" className="x" title="Cancel the order" onPointerDown={(e) => e.stopPropagation()} onClick={() => onCancel(o)}>✕</button>}
              </div>
            </div>
            {(["tp", "sl"] as const).map((k) => (o[k] != null || dragKey === `${o.id}:${k}`) && (
              <div key={k} ref={ref(`${o.id}:${k}`)} className={`ord-row lvl ${k}`} data-testid={`chart-order-${o.id}-${k}`}>
                <div className={`pos-tag lvl ${k}`} onPointerDown={(e) => start(e, o, k)} title={`Drag to move the ${k === "sl" ? "stop-loss" : "target"} (armed when the order fills)`}>
                  <span className="side">{k.toUpperCase()}</span>
                  <span className="px" />
                  {o[k] != null && <button type="button" className="x" title="Remove" onPointerDown={(e) => e.stopPropagation()} onClick={() => clear(o, k)}>✕</button>}
                </div>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
