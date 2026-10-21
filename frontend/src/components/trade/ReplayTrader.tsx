// ReplayTrader — buy / sell while Bar Replay runs, and the trades it books.
//
// Sits in the replay toolbar for one replay session. The chart's own symbol
// fills at the replayed bar's close; a future or option of it is priced by
// /api/replay/contract — that contract's real candles while it is still
// listed, else a model (fair-value future / Black-Scholes off the replayed
// candles) — and the panel says which. Nothing is sent to a broker.
//
// Leaving the replay (or picking a new start bar) closes whatever is open at
// its last price. Closed trades stay in this browser's replay journal
// (History), with totals and a CSV export.

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import { IST_OFFSET } from "./chartData";
import {
  advance, barBefore, closeAll, fill, journalCsv, loadJournal, mergeBars, newBook, openPnl, saveJournal, setLevels, stats,
  type RBar, type ReplayBook, type ReplayInstrument, type ReplayKind, type ReplayMark, type ReplayTrade,
} from "./replayBook";

const IST = IST_OFFSET;
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

interface ContractReply {
  ok: boolean;
  reason?: string;
  contract?: { kind: "FUT" | "CE" | "PE"; name: string; strike: number | null; expiry: number; label: string; lot: number; symbol: string | null };
  source?: "real" | "model";
  note?: string;
  candles?: number[][];
  more?: boolean;
  price_at_t?: number | null;
}

export interface ReplayTraderProps {
  symbol: string;
  shortName: string;
  /** Lot of the chart's own symbol (1 for cash / an index). */
  lot: number;
  /** The chart's candles (chart time) and the replayed bar's index in them. */
  candles: () => RBar[];
  idx: number;
  barSeconds: number;
  /** Base resolution the chart fetches; the contracts' candles use it too. */
  resolution: string;
  /** Offer its futures & options (an index / stock chart, not a contract). */
  fno: boolean;
  /** The replay is playing (the option quote is re-picked only while it isn't). */
  playing: boolean;
  onMarks: (m: ReplayMark[]) => void;
  fmt: (v: number) => string;
}

function toInst(r: ContractReply): ReplayInstrument | null {
  const c = r.contract;
  if (!r.ok || !c || !r.candles?.length) return null;
  return {
    key: `${c.kind}:${c.strike ?? ""}:${c.expiry}`, label: c.label, kind: c.kind, lot: Math.max(1, c.lot),
    source: r.source ?? "model", symbol: c.symbol, strike: c.strike, expiry: c.expiry + IST, more: !!r.more, note: r.note,
    bars: r.candles.map(([t, o, h, l, cl]) => ({ time: t + IST, open: o, high: h, low: l, close: cl })),
  };
}

async function getContract(q: Record<string, string | number | null | undefined>): Promise<ContractReply> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  return api.get<ContractReply>(`/api/replay/contract?${qs.toString()}`);
}

const errText = (e: unknown) => (e instanceof Error ? e.message : "request failed");

/** Chart time → "21 Oct 11:45" (chart times are IST already). */
export function fmtReplayTime(t: number): string {
  const d = new Date(t * 1000);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)} ${hh}:${mm}`;
}

function money(v: number): string {
  const s = `₹${Math.abs(Math.round(v)).toLocaleString("en-IN")}`;
  return v > 0 ? `+${s}` : v < 0 ? `−${s}` : s;
}

const pnlCls = (v: number) => (v > 0 ? "up" : v < 0 ? "down" : "");

function download(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function ReplayTrader(props: ReplayTraderProps) {
  const { symbol, shortName, lot, candles, idx, barSeconds, resolution, fno, playing, onMarks, fmt } = props;
  const cs = candles();
  const cur = cs[Math.min(idx, cs.length - 1)] as RBar | undefined;
  const nxt = cs[idx + 1] as RBar | undefined;
  /** The replay's "now": the replayed bar's close. */
  const now = cur ? (nxt ? Math.min(nxt.time, cur.time + barSeconds) : cur.time + barSeconds) : 0;
  const end = cs.length ? cs[cs.length - 1].time + barSeconds : now;

  const [book, setBook] = useState<ReplayBook>(() => newBook(symbol));
  const [journal, setJournal] = useState<ReplayTrade[]>(loadJournal);
  const [open, setOpen] = useState(true);
  const [tab, setTab] = useState<"positions" | "history">("positions");
  const [kind, setKind] = useState<ReplayKind>("SPOT");
  const [expiry, setExpiry] = useState<"current" | "next">("current");
  const [expiryKind, setExpiryKind] = useState<"weekly" | "monthly">("weekly");
  const [strikeMode, setStrikeMode] = useState<"ATM" | "ITM" | "OTM">("ATM");
  const [steps, setSteps] = useState(1);
  const [lots, setLots] = useState(1);
  const [slPts, setSlPts] = useState("");
  const [tpPts, setTpPts] = useState("");
  const [busy, setBusy] = useState<"BUY" | "SELL" | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  /** The contract BUY / SELL would trade, as resolved at replay time `at`. */
  const [preview, setPreview] = useState<{ inst: ReplayInstrument | null; reason?: string; loading: boolean; at?: number } | null>(null);

  const bookRef = useRef(book);
  bookRef.current = book;
  const nowRef = useRef(now);
  nowRef.current = now;
  const marksRef = useRef(onMarks);
  marksRef.current = onMarks;
  const loading = useRef(new Map<string, number>());

  const spot: ReplayInstrument = useMemo(
    () => ({ key: "chart", label: shortName, kind: "SPOT", lot: Math.max(1, lot), source: "chart", bars: [] }),
    [shortName, lot],
  );

  // Each replay step: stops / targets / marks over the bars it revealed.
  useEffect(() => {
    if (now) setBook((b) => advance(b, now, candles()));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [now]);

  useEffect(() => {
    marksRef.current(book.marks);
  }, [book.marks]);

  // Closed trades go to the journal as they close (a reload keeps them).
  useEffect(() => {
    if (book.trades.length) setJournal(saveJournal([...book.trades, ...loadJournal()]));
  }, [book.trades]);

  // The session ends with the replay: what is open closes at its last price.
  useEffect(
    () => () => {
      const b = closeAll(bookRef.current, nowRef.current, "REPLAY END");
      if (b.trades.length) saveJournal([...b.trades, ...loadJournal()]);
      marksRef.current([]);
    },
    [],
  );

  // A contract's candles come a stretch at a time: fetch the next one before the replay gets there.
  useEffect(() => {
    for (const pos of book.positions) {
      const inst = book.insts[pos.key];
      if (!inst || inst.source === "chart" || !inst.more || (loading.current.get(inst.key) ?? 0) > Date.now()) continue;
      const last = inst.bars.length ? inst.bars[inst.bars.length - 1].time : now;
      if (last > now + 20 * barSeconds) continue;
      loading.current.set(inst.key, Date.now() + 30_000);
      void getContract({
        underlying: symbol, kind: inst.kind, resolution, strike: inst.strike, expiry_ts: (inst.expiry ?? IST) - IST,
        t: last - IST + 1, from: last - IST + 1, to: Math.max(end, last + barSeconds) - IST,
      })
        .then((r) => {
          const more = toInst(r);
          setBook((b) => {
            const have = b.insts[inst.key];
            if (!have) return b;
            const next = more && more.source === have.source
              ? { ...have, bars: mergeBars(have.bars, more.bars), more: more.more }
              : { ...have, more: false };
            return advance({ ...b, insts: { ...b.insts, [inst.key]: next } }, nowRef.current, candles());
          });
          loading.current.delete(inst.key);
        })
        .catch(() => loading.current.set(inst.key, Date.now() + 10_000));   // retried in a bit
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [now, book.positions, book.insts]);

  const selection = (at: number, bar: RBar) => ({
    underlying: symbol, kind, resolution, expiry, expiry_kind: expiryKind, strike_mode: strikeMode,
    steps: strikeMode === "ATM" ? 0 : steps, t: at - IST, from: bar.time - IST, to: Math.max(end, at + barSeconds) - IST,
    spot: bar.close,
  });

  // The future / option BUY / SELL would trade and its price. Re-picked (the
  // ATM strike moves with the market) whenever the replay stands still; while
  // it plays, the quote runs on the contract already picked.
  const selKey = `${kind}|${expiry}|${expiryKind}|${strikeMode}|${steps}`;
  const previewKey = `${selKey}|${playing ? "playing" : now}`;
  useEffect(() => {
    if (kind === "SPOT" || !cur) {
      setPreview(null);
      return;
    }
    let dead = false;
    const at = now;
    setPreview((p) => ({ inst: p?.inst && p.inst.kind === kind ? p.inst : null, loading: true, at: p?.at }));
    const h = setTimeout(() => {
      getContract(selection(at, cur))
        .then((r) => !dead && setPreview({ inst: toInst(r), reason: r.ok ? undefined : r.reason, loading: false, at }))
        .catch((e) => !dead && setPreview({ inst: null, reason: errText(e), loading: false }));
    }, 300);
    return () => {
      dead = true;
      clearTimeout(h);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewKey]);

  const quoteInst = kind === "SPOT" ? spot : preview?.inst ?? null;
  const px = kind === "SPOT" ? cur?.close ?? null : quoteInst ? barBefore(quoteInst.bars, now)?.close ?? null : null;
  const unit = quoteInst?.lot ?? 1;

  function levels(side: "BUY" | "SELL", entry: number) {
    const sgn = side === "BUY" ? 1 : -1;
    const sl = Number(slPts), tp = Number(tpPts);
    const r2 = (x: number) => Math.round(x * 100) / 100;
    return {
      sl: slPts.trim() && sl > 0 && entry - sgn * sl > 0 ? r2(entry - sgn * sl) : null,
      tp: tpPts.trim() && tp > 0 && entry + sgn * tp > 0 ? r2(entry + sgn * tp) : null,
    };
  }

  async function place(side: "BUY" | "SELL"): Promise<void> {
    if (!cur || busy) return;
    setMsg(null);
    const at = now, bar = cur;
    const n = Math.max(1, Math.floor(lots || 1));
    if (kind === "SPOT") {
      setBook((b) => fill(b, spot, side, n * spot.lot, bar.close, at, bar.time, levels(side, bar.close)));
      return;
    }
    setBusy(side);
    try {
      // The quote on screen was picked at this very bar: trade it; else pick afresh.
      const ready = preview && !preview.loading && preview.at === at && preview.inst?.kind === kind ? preview.inst : null;
      const r = ready ? null : await getContract(selection(at, bar));
      const fresh = ready ?? (r ? toInst(r) : null);
      if (!fresh) {
        setMsg(r?.reason ?? "this contract can't be priced at the replayed time");
        return;
      }
      const b0 = bookRef.current;
      const have = b0.insts[fresh.key];
      const held = b0.positions.some((x) => x.key === fresh.key);
      // One series per contract while it is held: a fresh answer only extends it.
      const inst = have && held
        ? have.source === fresh.source ? { ...have, bars: mergeBars(have.bars, fresh.bars), more: fresh.more } : have
        : fresh;
      const bar0 = barBefore(inst.bars, at);
      if (!bar0) {
        setMsg(`${inst.label}: no price at the replayed time`);
        return;
      }
      setBook((b) => fill(b, inst, side, n * inst.lot, bar0.close, at, bar0.time, levels(side, bar0.close)));
      setPreview({ inst, loading: false, at });
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(null);
    }
  }

  const mtm = openPnl(book);
  const session = stats(book.trades);
  const all = stats(journal);
  const isOpt = kind === "CE" || kind === "PE";

  return (
    <div className="rtrade" data-testid="replay-trader">
      <div className="rtrade-ticket">
        <select value={kind} onChange={(e) => setKind(e.target.value as ReplayKind)} aria-label="Instrument" data-testid="rt-kind">
          <option value="SPOT">{shortName}</option>
          {fno && <option value="FUT">Future</option>}
          {fno && <option value="CE">Call (CE)</option>}
          {fno && <option value="PE">Put (PE)</option>}
        </select>
        {kind !== "SPOT" && (
          <select value={expiry} onChange={(e) => setExpiry(e.target.value as "current" | "next")} aria-label="Expiry" data-testid="rt-expiry">
            <option value="current">current expiry</option>
            <option value="next">next expiry</option>
          </select>
        )}
        {isOpt && (
          <>
            {/-INDEX$/i.test(symbol) && (
              <select value={expiryKind} onChange={(e) => setExpiryKind(e.target.value as "weekly" | "monthly")} aria-label="Weekly or monthly"
                title="weekly options trade on NIFTY and SENSEX; the other indices and stocks list monthlies">
                <option value="weekly">weekly</option>
                <option value="monthly">monthly</option>
              </select>
            )}
            <select value={strikeMode} onChange={(e) => setStrikeMode(e.target.value as "ATM" | "ITM" | "OTM")} aria-label="Strike" data-testid="rt-strike">
              <option value="ATM">ATM</option>
              <option value="ITM">ITM</option>
              <option value="OTM">OTM</option>
            </select>
            {strikeMode !== "ATM" && (
              <input type="number" min={1} max={50} value={steps} onChange={(e) => setSteps(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
                className="rt-num" title="strikes away from ATM" aria-label="Strikes away from ATM" />
            )}
          </>
        )}
        <label className="rt-lots" title={unit > 1 ? `lots of ${unit}` : "quantity"}>
          {unit > 1 || kind !== "SPOT" ? "lots" : "qty"}
          <input type="number" min={1} value={lots} onChange={(e) => setLots(Math.max(1, Math.floor(Number(e.target.value) || 1)))}
            className="rt-num" data-testid="rt-lots" />
        </label>
        <input type="number" min={0} placeholder="SL pts" value={slPts} onChange={(e) => setSlPts(e.target.value)} className="rt-num rt-lvl"
          title="stop loss, points from the fill" data-testid="rt-sl" />
        <input type="number" min={0} placeholder="TP pts" value={tpPts} onChange={(e) => setTpPts(e.target.value)} className="rt-num rt-lvl"
          title="target, points from the fill" data-testid="rt-tp" />
        <button type="button" className="rt-sell" disabled={!!busy || (kind === "SPOT" && px == null)} onClick={() => void place("SELL")} data-testid="rt-sell">
          {busy === "SELL" ? "…" : "SELL"} {px != null ? fmt(px) : ""}
        </button>
        <button type="button" className="rt-buy" disabled={!!busy || (kind === "SPOT" && px == null)} onClick={() => void place("BUY")} data-testid="rt-buy">
          {busy === "BUY" ? "…" : "BUY"} {px != null ? fmt(px) : ""}
        </button>
      </div>
      {kind !== "SPOT" && (
        <div className="rt-note" data-testid="rt-preview">
          {preview?.loading && !preview.inst ? "pricing…"
            : preview?.inst ? <>
              <b>{preview.inst.label}</b> · lot {preview.inst.lot} · <span className={preview.inst.source === "model" ? "rt-model" : "rt-real"}>
                {preview.inst.source === "real" ? "real candles" : "model price (estimate)"}</span>
              {preview.inst.note ? <span className="hint" title={preview.inst.note}> ⓘ</span> : null}
            </>
              : preview?.reason ?? ""}
        </div>
      )}
      {msg && <div className="rt-msg" data-testid="rt-msg">{msg}</div>}
      <div className="rt-sum" data-testid="rt-summary">
        <span>open {book.positions.length} · MTM <b className={pnlCls(mtm)}>{money(mtm)}</b></span>
        <span>· session {session.n} · net <b className={pnlCls(session.net)} data-testid="rt-session-net">{money(session.net)}</b></span>
        <span className="rt-tabs">
          <button type="button" className={open && tab === "positions" ? "on" : ""} onClick={() => { setTab("positions"); setOpen(true); }}>Positions</button>
          <button type="button" className={open && tab === "history" ? "on" : ""} onClick={() => { setTab("history"); setOpen(true); }} data-testid="rt-history-tab">History</button>
          <button type="button" onClick={() => setOpen(!open)} title={open ? "hide the lists" : "show the lists"}>{open ? "▾" : "▸"}</button>
        </span>
      </div>
      {open && tab === "positions" && (
        <div className="rt-list" data-testid="rt-positions">
          {!book.positions.length && !book.trades.length && <div className="hint">No replay trades yet — BUY / SELL fill at the replayed bar's close. Simulated: nothing goes to the broker.</div>}
          {book.positions.length > 0 && (
            <table>
              <thead><tr><th>Open</th><th>Qty</th><th>Avg</th><th>LTP</th><th>SL</th><th>TP</th><th>P&amp;L</th><th></th></tr></thead>
              <tbody>{book.positions.map((p) => {
                const v = (p.mark - p.avg) * p.qty;
                return (
                  <tr key={p.key} data-testid={`rt-pos-${p.key}`}>
                    <td>{p.label}</td>
                    <td className={p.qty > 0 ? "up" : "down"}>{p.qty > 0 ? "+" : ""}{p.qty}</td>
                    <td>{fmt(p.avg)}</td><td>{fmt(p.mark)}</td>
                    <td><LevelInput value={p.sl} label="Stop loss price" testId={`rt-pos-sl-${p.key}`}
                      onCommit={(v) => setBook((b) => setLevels(b, p.key, { sl: v }))} /></td>
                    <td><LevelInput value={p.tp} label="Target price" testId={`rt-pos-tp-${p.key}`}
                      onCommit={(v) => setBook((b) => setLevels(b, p.key, { tp: v }))} /></td>
                    <td className={pnlCls(v)}>{money(v)}</td>
                    <td><button type="button" title="close at the replayed bar's price" data-testid={`rt-close-${p.key}`}
                      onClick={() => setBook((b) => {
                        const pos = b.positions.find((x) => x.key === p.key);
                        if (!pos) return b;
                        const inst = b.insts[pos.key];
                        return fill(b, inst, pos.qty > 0 ? "SELL" : "BUY", Math.abs(pos.qty), pos.mark, now, pos.markAt);
                      })}>✕</button></td>
                  </tr>
                );
              })}</tbody>
            </table>
          )}
          {book.trades.length > 0 && <TradeTable trades={book.trades} fmt={fmt} />}
        </div>
      )}
      {open && tab === "history" && (
        <div className="rt-list" data-testid="rt-history">
          <div className="rt-stats">
            {all.n} trade{all.n === 1 ? "" : "s"} · {all.n ? Math.round((all.wins / all.n) * 100) : 0}% winners · net <b className={pnlCls(all.net)}>{money(all.net)}</b>
            {all.n > 0 && <> · best {money(all.best)} · worst {money(all.worst)}</>}
            <button type="button" disabled={!journal.length} onClick={() => download("replay-trades.csv", journalCsv(journal, fmtReplayTime))}>CSV</button>
            <button type="button" disabled={!journal.length} onClick={() => {
              if (!window.confirm("Clear the replay trade journal in this browser?")) return;
              setJournal(saveJournal([]));
            }}>Clear</button>
          </div>
          {journal.length ? <TradeTable trades={journal} fmt={fmt} chart /> : <div className="hint">Replay trades you close land here.</div>}
        </div>
      )}
    </div>
  );
}

/** A position's stop / target price, applied on Enter or leaving the box —
 *  never half-typed (a short's stop at "2" would fire on the next bar). */
function LevelInput({ value, label, testId, onCommit }: { value: number | null; label: string; testId: string; onCommit: (v: number | null) => void }) {
  const [draft, setDraft] = useState<string | null>(null);
  const commit = () => {
    if (draft === null) return;
    const v = draft.trim() === "" ? null : Number(draft);
    setDraft(null);
    if (v === null || (Number.isFinite(v) && v > 0)) onCommit(v);
  };
  return (
    <input type="number" className="rt-num rt-lvl" placeholder="—" aria-label={label} data-testid={testId}
      value={draft ?? (value ?? "")} onChange={(e) => setDraft(e.target.value)} onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        else if (e.key === "Escape") setDraft(null);
      }} />
  );
}

function TradeTable({ trades, fmt, chart = false }: { trades: ReplayTrade[]; fmt: (v: number) => string; chart?: boolean }) {
  return (
    <table>
      <thead><tr>{chart && <th>Chart</th>}<th>Closed</th><th>Instrument</th><th>Side</th><th>Qty</th><th>Entry → exit</th><th>Why</th><th>P&amp;L</th></tr></thead>
      <tbody>{trades.slice(0, 300).map((t) => (
        <tr key={t.id} title={`opened ${fmtReplayTime(t.entryAt)}${t.source === "model" ? " · model-priced (estimate)" : ""}`}>
          {chart && <td>{t.chart.split(":").pop()?.replace(/-(EQ|INDEX)$/, "")}</td>}
          <td>{fmtReplayTime(t.exitAt)}</td>
          <td>{t.label}{t.source === "model" ? <span className="rt-model" title="model price (estimate)"> ≈</span> : null}</td>
          <td className={t.side === "BUY" ? "up" : "down"}>{t.side === "BUY" ? "LONG" : "SHORT"}</td>
          <td>{t.qty}</td>
          <td>{fmt(t.entry)} → {fmt(t.exit)}</td>
          <td>{t.reason}</td>
          <td className={pnlCls(t.pnl)}>{money(t.pnl)}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}
