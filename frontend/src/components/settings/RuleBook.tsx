// Rule Book — a read-only reference card on the Settings page that answers
// "what can stop a trade?" without digging through code or docs.
//
// Layer 1 (signal rules) is fetched live so it never goes stale; the
// operator edits those on the Rules page. Layers 2-4 are the fixed,
// non-bypassable system gates — they only change if the engine changes.

import type { CSSProperties } from "react";

import { useRules } from "../../hooks/useApi";
import type { SignalCondition, SignalRule } from "../../types";

const OP_SYM: Record<string, string> = {
  "==": "=", "!=": "≠", ">=": "≥", "<=": "≤", ">": ">", "<": "<", in: "in",
};

function condText(c: SignalCondition): string {
  const v = Array.isArray(c.value) ? c.value.join(", ") : String(c.value);
  return `${c.field} ${OP_SYM[c.op] ?? c.op} ${v}`;
}

function summarize(rule: SignalRule): string {
  const all = (rule.conditions?.all_of ?? []).map(condText);
  const any = (rule.conditions?.any_of ?? []).map(condText);
  const parts: string[] = [];
  if (all.length) parts.push(`ALL of: ${all.join("  ·  ")}`);
  if (any.length) parts.push(`ANY of: ${any.join("  ·  ")}`);
  return parts.length ? parts.join("  |  ") : "no conditions";
}

// R0-R13 hard gates (app/risk/engine.py). Every signal passes through all
// of them; there is no override flag anywhere in the code.
const HARD_GATES: Array<{ id: string; name: string; blocks: string; knob?: string }> = [
  { id: "R0", name: "Stop-loss sanity", blocks: "SL missing, on the wrong side of entry, or nonsensical" },
  { id: "R1", name: "Action tradability", blocks: "action is HOLD or BLOCK — only BUY/SELL can trade" },
  { id: "R2", name: "Confidence floor", blocks: "confidence ≤ 0, or below the per-event floor (default 0.70)" },
  { id: "R3", name: "Minimum size", blocks: "computed quantity < 1 share" },
  { id: "R4", name: "Capital risk per trade", blocks: "risk exceeds the per-trade cap", knob: "MAX_CAPITAL_RISK_PCT" },
  { id: "R5", name: "Daily max loss", blocks: "today's realised loss would breach the daily cap", knob: "DAILY_MAX_LOSS_PCT" },
  { id: "R6", name: "Concurrent positions", blocks: "already at the open-position cap", knob: "MAX_CONCURRENT_POSITIONS" },
  { id: "R7", name: "Single-position size", blocks: "one name would exceed its share of the portfolio", knob: "MAX_SINGLE_POSITION_PCT" },
  { id: "R8", name: "Liquidity floor", blocks: "stock's traded value is below the minimum", knob: "MIN_LIQUIDITY_CRORE" },
  { id: "R9", name: "Sector concentration", blocks: "sector exposure would exceed its cap", knob: "SECTOR_CONCENTRATION_PCT" },
  { id: "R10", name: "Symbol blocklist", blocks: "symbol is on the blocklist" },
  { id: "R11", name: "Shorting toggle", blocks: "SELL signal while shorting is disabled" },
  { id: "R12", name: "Bid/ask spread", blocks: "spread wider than the cap", knob: "MAX_SPREAD_PCT" },
  { id: "R13", name: "Sector clustering", blocks: "2nd same-sector entry inside the window", knob: "SECTOR_CLUSTER_WINDOW_SECONDS" },
];

// Portfolio circuit breakers (app/risk/circuit_breakers.py). Trip state
// persists in the DB across restarts; manual kill clears only via Resume.
const BREAKERS: Array<{ name: string; trips: string }> = [
  { name: "DAILY LOSS", trips: "daily loss limit hit — halted for the day" },
  { name: "WEEKLY LOSS", trips: "weekly loss limit hit" },
  { name: "MONTHLY DRAWDOWN", trips: "monthly drawdown limit hit" },
  { name: "CONSECUTIVE LOSERS", trips: "losing-streak cooldown pause" },
  { name: "MAX TRADES / DAY", trips: "trade-count cap — no new entries today" },
  { name: "MANUAL KILL", trips: "kill switch — clears only via Resume" },
];

// Execution-layer guards (analyzer staleness gate + entry state machine +
// trade manager).
const GUARDS: Array<{ name: string; rule: string; knob?: string }> = [
  { name: "Staleness gate", rule: "news older than the freshness limit never trades", knob: "MAX_NEWS_AGE_SECONDS" },
  { name: "Entry drift (anti-chase)", rule: "price already ran past the intended entry — skip", knob: "MAX_ENTRY_DRIFT_PCT" },
  { name: "No live price", rule: "Fyers feed cold — block; never fill at a synthetic price" },
  { name: "Market hours", rule: "no entries outside the session entry window" },
  { name: "Time exit", rule: "positions force-closed after the max hold", knob: "MAX_HOLD_SECONDS" },
  { name: "EOD square-off", rule: "everything flattens before close — intraday only, no carry-forward" },
];

const sectionStyle: CSSProperties = { marginTop: 8 };
const summaryStyle: CSSProperties = { cursor: "pointer", userSelect: "none" };

export function RuleBook() {
  const { data: rules, isLoading } = useRules();

  return (
    <div className="widget" data-testid="rule-book">
      <h3>Rule Book — Quick Reference</h3>
      <p className="empty">
        Every filing passes 4 layers before it can trade. Layer 1 decides
        what <em>could</em> trade; layers 2–4 decide whether it&apos;s{" "}
        <em>allowed</em> — they are non-bypassable by design.
      </p>

      <details style={sectionStyle} open>
        <summary style={summaryStyle}>
          1 · Signal rules (yours — edit on the Rules page)
        </summary>
        {isLoading ? (
          <p className="empty">Loading…</p>
        ) : !rules || rules.length === 0 ? (
          <p className="empty">No signal rules defined — nothing can trade.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Rule</th>
                <th>Action</th>
                <th>Status</th>
                <th>Fires when</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.name}</td>
                  <td className="mono">{r.action}</td>
                  <td className={`mono ${r.enabled ? "" : "pnl-neg"}`}>
                    {r.enabled ? "LIVE" : "DISABLED"}
                  </td>
                  <td>{summarize(r)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </details>

      <details style={sectionStyle}>
        <summary style={summaryStyle}>
          2 · Hard risk gates R0–R13 (non-bypassable)
        </summary>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Gate</th>
              <th>Blocks when</th>
              <th>Setting</th>
            </tr>
          </thead>
          <tbody>
            {HARD_GATES.map((g) => (
              <tr key={g.id}>
                <td className="mono">{g.id}</td>
                <td>{g.name}</td>
                <td>{g.blocks}</td>
                <td className="mono">{g.knob ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>

      <details style={sectionStyle}>
        <summary style={summaryStyle}>
          3 · Circuit breakers (portfolio halts; survive restarts)
        </summary>
        <table>
          <thead>
            <tr>
              <th>Breaker</th>
              <th>Trips when</th>
            </tr>
          </thead>
          <tbody>
            {BREAKERS.map((b) => (
              <tr key={b.name}>
                <td className="mono">{b.name}</td>
                <td>{b.trips}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="empty">
          Trip history is in the Circuit Breaker History panel above; clear a
          halt from the Dashboard → Risk → Resume.
        </p>
      </details>

      <details style={sectionStyle}>
        <summary style={summaryStyle}>
          4 · Entry / exit guards (execution layer)
        </summary>
        <table>
          <thead>
            <tr>
              <th>Guard</th>
              <th>Rule</th>
              <th>Setting</th>
            </tr>
          </thead>
          <tbody>
            {GUARDS.map((g) => (
              <tr key={g.name}>
                <td>{g.name}</td>
                <td>{g.rule}</td>
                <td className="mono">{g.knob ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
