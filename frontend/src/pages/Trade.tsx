// Trade — the operator-driven single-page manual order ticket.
//
// Real-money only. Paper-mode broker accounts are filtered out of
// the account picker (the backend also rejects them with 400).
//
// Layout (Bloomberg-style, dense, dark):
//
//  ┌─────────────────────────────────────────────────────────────┐
//  │ SEARCH  [_______________]                                    │
//  │   results...                                                  │
//  ├──────────────────┬──────────────────┬──────────────────────┤
//  │ QUOTE            │ TICKET           │ PENDING ORDERS         │
//  │ symbol, ltp,     │ BUY / SELL       │ list of placed orders  │
//  │ bid/ask, vol     │ qty, type,       │ [cancel] per row       │
//  │                  │ product, SL/lim  │                        │
//  ├──────────────────┴──────────────────┴──────────────────────┤
//  │ OPTION CHAIN (only when symbol is an INDEX)                  │
//  │   expiry ▾   strike | CE bid/offer | PE bid/offer            │
//  │           ...                                                  │
//  └─────────────────────────────────────────────────────────────┘
//
// Soft risk: if the risk engine blocks, the ticket surfaces the
// blocking codes and a "BYPASS" field that the operator must type
// "I ACCEPT THE RISK" into before re-submitting.

import { useEffect, useMemo, useState } from "react";
import {
  useBrokerAccounts,
  useCancelOrder,
  useOptionChain,
  usePendingOrders,
  usePlaceOrder,
  usePositions,
  useQuote,
  useRefreshInstruments,
  useSearchSymbols,
  useServerInfo,
} from "../hooks/useApi";
import { useLiveQuote } from "../hooks/useQuotes";
import ChartPanel, { type BrokerLine } from "../components/trade/ChartPanel";
import type {
  BrokerAccount,
  InstrumentHit,
  OptionLeg,
  OrderType,
  PlaceOrderRequest,
  Position,
  ProductType,
} from "../types";

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

// One open-position row. Overlays the live `/ws` mark over the 5s REST
// poll and recomputes unrealised P&L from it — (last - avg) * qty handles
// both long and short (qty is negative for shorts). Falls back to the REST
// values until the first tick for this symbol streams in.
function PositionRow({ p }: { p: Position }) {
  const live = useLiveQuote(p.symbol);
  const ltp = live?.last_price ?? p.last_price;
  const pnl =
    live?.last_price != null
      ? (live.last_price - p.average_price) * p.quantity
      : p.unrealized_pnl;
  return (
    <tr>
      <td className="sym">{p.symbol}</td>
      <td className={p.quantity > 0 ? "up" : "down"}>{p.quantity}</td>
      <td>{fmtMoney(p.average_price)}</td>
      <td>{fmtMoney(ltp)}</td>
      <td className={(pnl ?? 0) >= 0 ? "up" : "down"}>{fmtMoney(pnl)}</td>
    </tr>
  );
}

/** Compact open-interest formatter (Indian units): K / L (lakh) / Cr. */
function fmtOi(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (v >= 1e7) return (v / 1e7).toFixed(2) + "Cr";
  if (v >= 1e5) return (v / 1e5).toFixed(2) + "L";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(v);
}

/** Make an error string operator-friendly. We never want to dump
 *  a Cloudflare challenge page or a stacktrace into the orange
 *  banner. Strips HTML, collapses whitespace, caps length, and
 *  falls back to a generic message if we somehow ended up with
 *  empty input. Used by both the place-order and cancel flows. */
function cleanError(raw: unknown, fallback: string): string {
  if (raw == null) return fallback;
  let s = typeof raw === "string" ? raw : String(raw);
  // Drop HTML tags + decode the most common entities. A regex is
  // good enough for operator-visible text — we're not sanitising
  // user input, just stripping an upstream CDN's challenge page.
  s = s
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
  if (!s) return fallback;
  // Cap length to keep the banner compact. The HTML-strip above
  // already neutralises CDN challenge pages, so this is only a
  // secondary guard — 320 leaves room for the broker's full
  // diagnostic (e.g. the -50 app-rejection guidance) without
  // truncating it mid-sentence.
  if (s.length > 320) s = s.slice(0, 320) + "…";
  return s;
}

// Recently charted symbols (TradingView-watchlist-style quick switch).
// Kept tiny and local: the last 8 instruments picked on this page.
const RECENT_KEY = "trade:recent";

function loadRecent(): InstrumentHit[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    const list = raw ? (JSON.parse(raw) as InstrumentHit[]) : [];
    return Array.isArray(list) ? list.filter((h) => h && h.symbol) : [];
  } catch {
    return [];
  }
}

export default function Trade() {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<InstrumentHit | null>(null);
  const [showResults, setShowResults] = useState(false);
  // Keyboard cursor into the search results (-1 = nothing highlighted).
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const [recent, setRecent] = useState<InstrumentHit[]>(loadRecent);

  const { data: accounts } = useBrokerAccounts();
  // Trade page is REAL-MONEY ONLY. Filter out paper-mode rows.
  // We silently pick the first real account — there's no UI switcher.
  const realAccounts = useMemo<BrokerAccount[]>(
    () => (accounts?.filter((a) => !a.paper_mode && a.enabled) ?? []),
    [accounts],
  );
  const accountId: number | null = realAccounts.length > 0 ? realAccounts[0].id : null;
  // Server identity (public IP). Surfaced in the place-order
  // error banner when Fyers rejects with the IP-whitelist
  // error so the operator can copy the IP into the Fyers app
  // dashboard's whitelist.
  const { data: serverInfo } = useServerInfo();

  const { data: searchData, isFetching: searching } = useSearchSymbols(query);
  const refreshInstruments = useRefreshInstruments();

  const { data: quote } = useQuote(selected?.symbol ?? "");
  // Sub-second overlay: the `/ws` push stream (touched live by the quote
  // endpoint above) overrides the 3s REST poll the instant ticks arrive.
  const live = useLiveQuote(selected?.symbol);
  const ltp = live?.last_price ?? (quote?.ok ? quote.last_price : null);
  const bid = live?.bid ?? (quote?.ok ? quote.bid : null);
  const ask = live?.ask ?? (quote?.ok ? quote.ask : null);
  // Selected option-chain expiry (epoch ts). Reset on each new symbol.
  const [selectedExpiry, setSelectedExpiry] = useState<string | null>(null);
  // The chain is available for index underlyings AND F&O stocks. We fetch
  // for both indices and cash equities; a non-F&O stock just returns no
  // strikes and the panel stays hidden.
  const chainEligible =
    selected != null &&
    (selected.instrument_type === "IND" || selected.instrument_type === "EQ");
  const { data: chain } = useOptionChain(
    chainEligible ? selected!.symbol : "",
    chainEligible ? selected!.short_name : "",
    selectedExpiry,
    12,
  );
  const { data: pending } = usePendingOrders(accountId);
  const { data: positions } = usePositions();

  // --- Ticket state ---
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [quantity, setQuantity] = useState<number>(1);
  const [orderType, setOrderType] = useState<OrderType>("MARKET");
  const [productType, setProductType] = useState<ProductType>("INTRADAY");
  const [limitPrice, setLimitPrice] = useState<string>("");
  const [stopPrice, setStopPrice] = useState<string>("");
  const [lastResult, setLastResult] = useState<{
    type: "success" | "error";
    message: string;
    warning?: string | null;
    detail?: PlaceOrderRequest;
    // Diagnostic reason from the Fyers backend. Set when the
    // place-order rejection has a known cause the UI can
    // surface as a richer banner — currently just
    // "ip_whitelist" (Fyers code -50: "Algo orders are not
    // allowed from this app", which is the misleading message
    // Fyers uses when the server's IP isn't on the app's
    // IP-whitelist). When set, the banner also shows the
    // bot's public IP so the operator can copy it into the
    // Fyers dashboard.
    reason?: string | null;
  } | null>(null);
  // Tracks the most recent cancel attempt so the operator gets
  // feedback in the UI. The pending list disappearing is implicit
  // confirmation; an explicit message covers the case where the
  // broker says "already gone" (the row is no longer in the list,
  // but the operator wants to know why their click "did nothing").
  const [cancelMessage, setCancelMessage] = useState<{
    type: "success" | "info" | "error";
    text: string;
  } | null>(null);

  // Lot-size aware default qty
  useEffect(() => {
    if (selected && selected.lot_size > 1 && quantity === 1) {
      setQuantity(selected.lot_size);
    }
  }, [selected, quantity]);

  const placeOrder = usePlaceOrder();
  const cancelOrder = useCancelOrder();

  // Broker state drawn on the chart: open position average + pending
  // order prices for the charted symbol.
  const brokerLines = useMemo<BrokerLine[]>(() => {
    if (!selected) return [];
    const lines: BrokerLine[] = [];
    const pos = positions?.find(
      (p) => p.symbol === selected.symbol && p.quantity !== 0,
    );
    if (pos) {
      lines.push({
        price: pos.average_price,
        title: `pos ${pos.quantity > 0 ? "+" : ""}${pos.quantity}`,
        kind: pos.quantity > 0 ? "position-long" : "position-short",
      });
    }
    for (const o of pending?.orders ?? []) {
      if (o.symbol === selected.symbol && o.price > 0) {
        lines.push({
          price: o.price,
          title: `${o.side} ${o.quantity} ${o.order_type}`,
          kind: "order",
        });
      }
    }
    return lines;
  }, [selected, positions, pending]);

  // Chart "⤷ Ticket" tool → prefill the ticket as a LIMIT order.
  const onPickPrice = (price: number) => {
    setOrderType("LIMIT");
    setLimitPrice(price.toFixed(2));
  };

  const isOption =
    selected != null && (selected.instrument_type === "CE" || selected.instrument_type === "PE");
  const isFuture = selected != null && selected.instrument_type === "FUT";

  // Fyers v3 price rules: LIMIT and STOP_LOSS (SL-L, stop-limit) both
  // need a limit price; STOP_LOSS (SL-L) and SL-M both need a stop /
  // trigger price. MARKET needs neither.
  const requiresLimit = orderType === "LIMIT" || orderType === "STOP_LOSS";
  const requiresStop = orderType === "STOP_LOSS" || orderType === "SL-M";
  const canSubmit = useMemo(() => {
    if (!selected || !accountId) return false;
    if (quantity <= 0) return false;
    if (requiresLimit && !limitPrice) return false;
    if (requiresStop && !stopPrice) return false;
    return true;
  }, [selected, accountId, quantity, requiresLimit, limitPrice, requiresStop, stopPrice]);

  const onSelect = (h: InstrumentHit) => {
    setSelected(h);
    setShowResults(false);
    setQuery(h.display);
    setLastResult(null);
    setSelectedExpiry(null); // load the nearest expiry for the new symbol
    setRecent((prev) => {
      const next = [h, ...prev.filter((r) => r.symbol !== h.symbol)].slice(0, 8);
      try {
        localStorage.setItem(RECENT_KEY, JSON.stringify(next));
      } catch {
        /* persistence is best-effort */
      }
      return next;
    });
    if (h.instrument_type === "CE" || h.instrument_type === "PE") {
      setOrderType("MARKET");
      setProductType("NORMAL"); // F&O options default to NRML
    } else if (h.instrument_type === "FUT") {
      setOrderType("MARKET");
      setProductType("NORMAL");
    } else {
      setOrderType("MARKET");
      setProductType("INTRADAY");
    }
  };

  // The strike nearest the spot — highlighted as ATM in the chain.
  const atmStrike = useMemo<number | null>(() => {
    if (!chain || chain.spot == null || chain.strikes.length === 0) return null;
    let best = chain.strikes[0].strike;
    let bestDist = Infinity;
    for (const s of chain.strikes) {
      const d = Math.abs(s.strike - chain.spot);
      if (d < bestDist) {
        bestDist = d;
        best = s.strike;
      }
    }
    return best;
  }, [chain]);

  // Click a CE/PE cell in the chain → load that option into the ticket.
  const onSelectOption = (
    row: { strike: number; ce: OptionLeg | null; pe: OptionLeg | null },
    type: "CE" | "PE",
  ) => {
    const leg = type === "CE" ? row.ce : row.pe;
    if (!leg || !selected) return;
    const exch = leg.symbol.includes(":") ? leg.symbol.split(":")[0] : selected.exchange;
    onSelect({
      symbol: leg.symbol,
      short_name: `${selected.short_name} ${row.strike} ${type}`,
      exchange: exch,
      segment: "FO",
      instrument_type: type,
      lot_size: leg.lot_size,
      tick_size: leg.tick_size,
      expiry: null,
      strike: row.strike,
      underlying: selected.short_name,
      display: `${selected.short_name} ${row.strike} ${type}`,
    });
  };

  const onSubmit = async (opts?: { bypassRisk?: boolean }) => {
    if (!selected || !accountId) return;
    const body: PlaceOrderRequest = {
      account_id: accountId,
      symbol: selected.symbol,
      side,
      quantity: Number(quantity),
      order_type: orderType,
      limit_price: requiresLimit && limitPrice ? Number(limitPrice) : null,
      stop_price: requiresStop && stopPrice ? Number(stopPrice) : null,
      product_type: productType,
      bypass_risk: opts?.bypassRisk ?? false,
      operator: "ui_trade_page",
    };
    try {
      const r = await placeOrder.mutateAsync(body);
      if (r.status === "REJECTED" || r.status === "REJECTED_RISK" || r.ok === false) {
        setLastResult({
          type: "error",
          message: cleanError(
            r.error || r.risk_message,
            "broker rejected the order",
          ),
          detail: body,
          reason: r.reason ?? null,
        });
      } else {
        setLastResult({
          type: "success",
          message: `${r.status}  ${selected.symbol}  ${side} ${quantity}  @ ${r.broker_order_id ?? "—"}`,
          // Risk is advisory for manual orders — surface it without blocking.
          warning: r.risk_warning ?? (r.risk_message ? r.risk_message : null),
          detail: body,
        });
      }
    } catch (e) {
      setLastResult({
        type: "error",
        message: cleanError(
          (e as Error).message,
          "place-order request failed",
        ),
        detail: body,
      });
    }
  };

  const onCancel = async (brokerOrderId: string) => {
    if (!accountId) return;
    setCancelMessage(null);
    try {
      const r = await cancelOrder.mutateAsync({
        account_id: accountId,
        broker_order_id: brokerOrderId,
      });
      // Broker can return ok:false when the order is already gone
      // (filled, rejected, cancelled, or simply not on the broker
      // anymore). The backend still drops it from our local pending
      // list, so surface the broker's "no" to the operator so the
      // click doesn't look silently broken.
      if (r.ok) {
        setCancelMessage({
          type: "success",
          text: `Cancelled ${brokerOrderId}`,
        });
      } else {
        setCancelMessage({
          type: "info",
          text: `Broker rejected cancel for ${brokerOrderId} — order was already gone. Removed from pending list.`,
        });
      }
    } catch (e) {
      setCancelMessage({
        type: "error",
        text: `Cancel failed for ${brokerOrderId}: ${cleanError(
          (e as Error).message,
          "request failed",
        )}`,
      });
    }
  };

  // -------- render --------

  const noRealAccounts = realAccounts.length === 0;

  return (
    <div className="trade-page">
      <h1 className="page-title">Trade</h1>
      {noRealAccounts && (
        <div className="warn">
          No live broker account. Add one in the <b>Accounts</b> page and
          complete OAuth.
        </div>
      )}

      {/* ---- search ---- */}
      <div className="trade-search">
        <div className="trade-search-bar">
          <input
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setShowResults(true);
              setHighlightIdx(-1);
              setLastResult(null);
            }}
            onFocus={() => {
              // Reopen only for a genuine query — not for the display
              // string a selection left behind (which has no hits and
              // would show a stale "no results" box).
              if (query && query !== selected?.display) setShowResults(true);
            }}
            onKeyDown={(e) => {
              const hits = searchData?.hits ?? [];
              if (e.key === "Escape") {
                setShowResults(false);
                setHighlightIdx(-1);
                return;
              }
              if (!showResults || hits.length === 0) return;
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setHighlightIdx((i) => (i + 1) % hits.length);
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setHighlightIdx((i) => (i <= 0 ? hits.length - 1 : i - 1));
              } else if (e.key === "Enter") {
                e.preventDefault();
                const pick = hits[highlightIdx >= 0 ? highlightIdx : 0];
                if (pick) {
                  onSelect(pick);
                  setHighlightIdx(-1);
                }
              }
            }}
            placeholder="Search symbol — e.g. RELIANCE, NIFTY, BANKNIFTY"
            data-testid="trade-search"
            autoComplete="off"
          />
          <button
            type="button"
            className="btn-sm"
            onClick={() => refreshInstruments.mutate()}
            disabled={refreshInstruments.isPending}
            data-testid="refresh-instruments"
            title="Download the full NSE + BSE stock list from Fyers"
          >
            {refreshInstruments.isPending
              ? "loading…"
              : refreshInstruments.isSuccess
              ? `✓ ${refreshInstruments.data?.instrument_count?.toLocaleString() ?? ""} symbols`
              : "↻ Load all NSE/BSE"}
          </button>
        </div>
        {recent.length > 0 && (
          <div className="trade-recent" data-testid="trade-recent">
            <span className="trade-recent-label">recent</span>
            {recent.map((h) => (
              <button
                key={h.symbol}
                type="button"
                className={`trade-recent-chip${selected?.symbol === h.symbol ? " on" : ""}`}
                onClick={() => onSelect(h)}
                title={h.symbol}
                data-testid={`recent-chip-${h.symbol}`}
              >
                {h.short_name}
              </button>
            ))}
          </div>
        )}
        {showResults && query && (
          <div className="trade-search-results" data-testid="trade-search-results">
            {searching && <div className="hint">searching…</div>}
            {!searching && (searchData?.count ?? 0) === 0 && (
              <div className="hint">no results for "{query}"</div>
            )}
            {(searchData?.hits ?? []).map((h, i) => (
              <button
                key={h.symbol}
                type="button"
                className={`trade-search-row${i === highlightIdx ? " hl" : ""}`}
                onClick={() => onSelect(h)}
                onMouseEnter={() => setHighlightIdx(i)}
                data-testid={`search-row-${h.symbol}`}
              >
                <span className="sym">{h.short_name}</span>
                <span className="exch">{h.exchange}:{h.segment}</span>
                <span className="disp">{h.display}</span>
                {h.lot_size > 1 && <span className="lot">lot {h.lot_size}</span>}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* ---- TradingView-style chart for the selected instrument ----
           key={symbol} remounts the panel per symbol so its internal
           candle store, drawings, and pagination reset cleanly. */}
      {selected && (
        <ChartPanel
          key={selected.symbol}
          symbol={selected.symbol}
          shortName={selected.short_name}
          brokerLines={brokerLines}
          onPickPrice={onPickPrice}
        />
      )}

      {/* ---- main grid: quote | ticket | pending ---- */}
      <div className="trade-grid">
        {/* QUOTE */}
        <section className="trade-card" data-testid="trade-quote">
          <h2>Quote</h2>
          {!selected ? (
            <div className="empty">Pick a symbol above to start.</div>
          ) : (
            <div>
              <div className="quote-sym">
                <span className="big">{selected.short_name}</span>
                <span className="exch">{selected.exchange}:{selected.segment}</span>
                {isOption && <span className="badge opt">OPTION</span>}
                {isFuture && <span className="badge fut">FUTURE</span>}
                {!isOption && !isFuture && <span className="badge cash">CASH</span>}
              </div>
              <div className="quote-fullsym">{selected.symbol}</div>
              <div className="quote-row">
                <div className="quote-cell">
                  <div className="k">LTP</div>
                  <div className="v big" data-testid="quote-ltp">
                    {ltp != null ? fmtMoney(ltp) : "—"}
                  </div>
                </div>
                <div className="quote-cell">
                  <div className="k">BID</div>
                  <div className="v">{bid != null ? fmtMoney(bid) : "—"}</div>
                </div>
                <div className="quote-cell">
                  <div className="k">ASK</div>
                  <div className="v">{ask != null ? fmtMoney(ask) : "—"}</div>
                </div>
                <div className="quote-cell">
                  <div className="k">LOT</div>
                  <div className="v">{selected.lot_size}</div>
                </div>
              </div>
              {!quote?.ok && (
                <div className="hint warn-text">
                  {quote?.reason ?? "no quote cached for this symbol"}
                </div>
              )}
            </div>
          )}
        </section>

        {/* TICKET */}
        <section className="trade-card" data-testid="trade-ticket">
          <h2>Ticket</h2>
          <div className="ticket-side" role="group" aria-label="side">
            <button
              className={`ticket-side-btn buy ${side === "BUY" ? "on" : ""}`}
              onClick={() => setSide("BUY")}
              data-testid="ticket-side-buy"
            >
              BUY
            </button>
            <button
              className={`ticket-side-btn sell ${side === "SELL" ? "on" : ""}`}
              onClick={() => setSide("SELL")}
              data-testid="ticket-side-sell"
            >
              SELL
            </button>
          </div>

          <label className="ticket-row">
            <span>Quantity</span>
            <input
              type="number"
              min={1}
              step={selected?.lot_size ?? 1}
              value={quantity}
              onChange={(e) => setQuantity(Math.max(1, Number(e.target.value || 1)))}
              data-testid="ticket-qty"
            />
            {selected && selected.lot_size > 1 && (
              <span className="hint">
                {Math.floor(quantity / selected.lot_size)} lot(s)
              </span>
            )}
          </label>

          <label className="ticket-row">
            <span>Order type</span>
            <select
              value={orderType}
              onChange={(e) => setOrderType(e.target.value as OrderType)}
              data-testid="ticket-type"
            >
              <option value="MARKET">MARKET</option>
              <option value="LIMIT">LIMIT</option>
              <option value="STOP_LOSS">STOP_LOSS (SL-L)</option>
              <option value="SL-M">SL-M</option>
            </select>
          </label>

          {requiresLimit && (
            <label className="ticket-row">
              <span>Limit price</span>
              <input
                type="number"
                step="0.05"
                value={limitPrice}
                onChange={(e) => setLimitPrice(e.target.value)}
                data-testid="ticket-limit"
              />
              <span className="px-quick">
                {bid != null && (
                  <button
                    type="button"
                    className="px-quick-btn"
                    onClick={() => setLimitPrice(bid.toFixed(2))}
                    title={`bid ${fmtMoney(bid)}`}
                    data-testid="limit-fill-bid"
                  >
                    bid
                  </button>
                )}
                {ltp != null && (
                  <button
                    type="button"
                    className="px-quick-btn"
                    onClick={() => setLimitPrice(ltp.toFixed(2))}
                    title={`LTP ${fmtMoney(ltp)}`}
                    data-testid="limit-fill-ltp"
                  >
                    ltp
                  </button>
                )}
                {ask != null && (
                  <button
                    type="button"
                    className="px-quick-btn"
                    onClick={() => setLimitPrice(ask.toFixed(2))}
                    title={`ask ${fmtMoney(ask)}`}
                    data-testid="limit-fill-ask"
                  >
                    ask
                  </button>
                )}
              </span>
            </label>
          )}

          {requiresStop && (
            <label className="ticket-row">
              <span>Stop price</span>
              <input
                type="number"
                step="0.05"
                value={stopPrice}
                onChange={(e) => setStopPrice(e.target.value)}
                data-testid="ticket-stop"
              />
              {ltp != null && (
                <span className="px-quick">
                  <button
                    type="button"
                    className="px-quick-btn"
                    onClick={() => setStopPrice(ltp.toFixed(2))}
                    title={`LTP ${fmtMoney(ltp)}`}
                    data-testid="stop-fill-ltp"
                  >
                    ltp
                  </button>
                </span>
              )}
            </label>
          )}

          <label className="ticket-row">
            <span>Product</span>
            {/* Intraday-only bot: no delivery / carry-forward, ever. The
                backend rejects anything else, so don't offer it. */}
            <select
              value={productType}
              onChange={(e) => setProductType(e.target.value as ProductType)}
              data-testid="ticket-product"
              disabled
              title="Intraday-only bot — every position is squared off the same day"
            >
              <option value="INTRADAY">INTRADAY (MIS)</option>
            </select>
          </label>

          {/* Submit */}
          <div className="ticket-submit">
            <button
              className={`btn ${side === "BUY" ? "buy" : "sell"}`}
              disabled={!canSubmit || placeOrder.isPending}
              onClick={() => onSubmit()}
              data-testid="ticket-submit"
            >
              {placeOrder.isPending ? "placing…" : `PLACE ${side}`}
            </button>
          </div>

          {lastResult && lastResult.type === "success" && (
            <div className="result success" data-testid="ticket-result-success">
              {lastResult.message}
              {lastResult.warning && (
                <div className="result-warning" data-testid="ticket-risk-advisory">
                  ⚠ risk advisory: {lastResult.warning}
                </div>
              )}
            </div>
          )}
          {lastResult && lastResult.type === "error" && (
            <div className="result error" data-testid="ticket-result-error">
              {lastResult.message}
              {lastResult.reason === "risk_block" && (
                <div
                  className="risk-override-hint"
                  data-testid="ticket-risk-override"
                  style={{
                    marginTop: 10,
                    padding: "8px 10px",
                    border: "1px solid var(--amber)",
                    borderRadius: 4,
                    background: "rgba(255, 200, 80, 0.08)",
                    fontSize: 12,
                  }}
                >
                  <div style={{ marginBottom: 8 }}>
                    This order breaches a limit set in <b>Settings</b>. Reduce
                    the size or relax the limit — or override the check and
                    place it anyway.
                  </div>
                  <button
                    className="btn-sm sell"
                    onClick={() => onSubmit({ bypassRisk: true })}
                    disabled={placeOrder.isPending}
                    data-testid="ticket-risk-override-btn"
                  >
                    {placeOrder.isPending
                      ? "placing…"
                      : "⚠ Override risk & place anyway"}
                  </button>
                </div>
              )}
              {lastResult.reason === "ip_whitelist" && serverInfo?.public_ip && (
                <div
                  className="ip-whitelist-hint"
                  data-testid="ticket-ip-whitelist"
                  style={{
                    marginTop: 10,
                    padding: "8px 10px",
                    border: "1px solid var(--amber)",
                    borderRadius: 4,
                    background: "rgba(255, 200, 80, 0.08)",
                    fontSize: 12,
                    fontFamily: "var(--mono)",
                  }}
                >
                  <div style={{ marginBottom: 6 }}>
                    Add this server's public IP to the Fyers app's
                    IP-whitelist on{" "}
                    <a
                      href="https://myapi.fyers.in/dashboard/"
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: "var(--amber)" }}
                    >
                      myapi.fyers.in/dashboard
                    </a>
                    , then click PLACE BUY again.
                  </div>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                    }}
                  >
                    <div
                      style={{
                        fontSize: 14,
                        fontWeight: 700,
                        letterSpacing: "0.05em",
                        userSelect: "all",
                        flex: 1,
                      }}
                      data-testid="ticket-server-ip"
                    >
                      {serverInfo.public_ip}
                    </div>
                    <button
                      className="btn-sm"
                      onClick={() => {
                        if (serverInfo.public_ip && navigator.clipboard) {
                          navigator.clipboard
                            .writeText(serverInfo.public_ip)
                            .catch(() => {
                              /* ignore — the IP is selectable */
                            });
                        }
                      }}
                      data-testid="ticket-server-ip-copy"
                      title="Copy IP"
                    >
                      Copy
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>

        {/* PENDING ORDERS */}
        <section className="trade-card" data-testid="trade-pending">
          <h2>Pending orders</h2>
          {cancelMessage && (
            <div
              className={`result ${cancelMessage.type}`}
              data-testid="cancel-result"
            >
              {cancelMessage.text}
            </div>
          )}
          {(pending?.count ?? 0) === 0 ? (
            <div className="empty">No pending orders.</div>
          ) : (
            <table className="pending-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Qty</th>
                  <th>Type</th>
                  <th>ID</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {pending!.orders.map((o) => {
                  // The mutation's `variables` carries the request body
                  // while it's in flight; use it to disable just the
                  // button that was clicked so the rest of the rows
                  // stay clickable.
                  const pendingId =
                    cancelOrder.isPending &&
                    cancelOrder.variables?.broker_order_id === o.broker_order_id
                      ? o.broker_order_id
                      : null;
                  return (
                    <tr key={o.id}>
                      <td className="sym">{o.symbol}</td>
                      <td className={o.side === "BUY" ? "up" : "down"}>{o.side}</td>
                      <td>{o.quantity}</td>
                      <td>{o.order_type}</td>
                      <td className="broker-id">{o.broker_order_id ?? "—"}</td>
                      <td>
                        {o.broker_order_id && (
                          <button
                            className="btn small"
                            onClick={() => onCancel(o.broker_order_id!)}
                            disabled={pendingId !== null}
                            data-testid={`cancel-${o.broker_order_id}`}
                          >
                            {pendingId !== null ? "cancelling…" : "Cancel"}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>
      </div>

      {/* ---- option chain (index underlyings + F&O stocks) ----
           Indices always show the panel; a cash stock shows it only when
           it actually has options (F&O stock), so non-F&O names stay clean. */}
      {selected &&
        (selected.instrument_type === "IND" ||
          (selected.instrument_type === "EQ" &&
            (chain?.strikes?.length ?? 0) > 0)) && (
        <section className="trade-card chain" data-testid="trade-chain">
          <div className="chain-head">
            <h2>Option chain — {selected.short_name}</h2>
            <div className="chain-meta">
              <span
                className={`badge ${chain?.source === "fyers" ? "cash" : "neutral"}`}
                title={chain?.source === "fyers" ? "live Fyers prices" : "static ladder"}
              >
                {chain?.source === "fyers" ? "LIVE" : "STATIC"}
              </span>
              {chain?.spot != null && (
                <span data-testid="chain-spot">spot {fmtMoney(chain.spot)}</span>
              )}
              {(chain?.expiries?.length ?? 0) > 0 && (
                <label className="chain-expiry">
                  <span>expiry</span>
                  <select
                    value={selectedExpiry ?? chain?.selected_expiry ?? ""}
                    onChange={(e) => setSelectedExpiry(e.target.value || null)}
                    data-testid="chain-expiry"
                  >
                    {chain!.expiries.map((ex) => (
                      <option key={ex.ts} value={ex.ts}>
                        {ex.label}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
          </div>
          {!chain ? (
            <div className="empty">Loading chain…</div>
          ) : chain.strikes.length === 0 ? (
            <div className="empty" data-testid="chain-empty">
              {chain.reason ?? "No option chain available for this underlying."}
            </div>
          ) : (
            <table className="chain-table">
              <thead>
                <tr>
                  <th colSpan={2} className="ce-head">CALLS (CE)</th>
                  <th className="strike-head">Strike</th>
                  <th colSpan={2} className="pe-head">PUTS (PE)</th>
                </tr>
                <tr className="chain-subhead">
                  <th>OI</th>
                  <th>LTP</th>
                  <th> </th>
                  <th>LTP</th>
                  <th>OI</th>
                </tr>
              </thead>
              <tbody>
                {chain.strikes.map((s) => {
                  const atm = s.strike === atmStrike;
                  return (
                    <tr key={s.strike} className={atm ? "atm" : ""}>
                      <td className="oi">{fmtOi(s.ce?.oi)}</td>
                      <td className="ce-cell">
                        {s.ce ? (
                          <button
                            className="chain-ltp ce"
                            onClick={() => onSelectOption(s, "CE")}
                            data-testid={`chain-ce-${s.strike}`}
                            title={s.ce.symbol}
                          >
                            {s.ce.ltp != null ? fmtMoney(s.ce.ltp) : "—"}
                          </button>
                        ) : (
                          "—"
                        )}
                      </td>
                      <th className="strike">
                        {s.strike}
                        {atm && <span className="atm-tag">ATM</span>}
                      </th>
                      <td className="pe-cell">
                        {s.pe ? (
                          <button
                            className="chain-ltp pe"
                            onClick={() => onSelectOption(s, "PE")}
                            data-testid={`chain-pe-${s.strike}`}
                            title={s.pe.symbol}
                          >
                            {s.pe.ltp != null ? fmtMoney(s.pe.ltp) : "—"}
                          </button>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="oi">{fmtOi(s.pe?.oi)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>
      )}

      {/* ---- open positions (mini panel) ---- */}
      {positions && positions.length > 0 && (
        <section className="trade-card" data-testid="trade-positions">
          <h2>Open positions</h2>
          <table className="positions-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Qty</th>
                <th>Avg</th>
                <th>LTP</th>
                <th>P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <PositionRow key={p.symbol} p={p} />
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
