// News page: a live headline monitor (Moneycontrol, NDTV, Zee Business, CNBC-TV18, ET, Mint, BusinessLine, BSE
// + your own feeds). Monitoring only: nothing here is sent to the analyzer or the AI.
// Server-side settings (what is collected, how often, alerts) live in /api/news/settings; per-browser
// conveniences (sound, desktop notifications, view) live in localStorage.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { Toggle } from "../components/common/Toggle";
import { Check, Modal, Num, Row, Section, Sel } from "../components/trade/chartUi";

interface Extract { status: string | null; chars: number; error: string | null; fetched?: number | null }
interface Lag { p50: number | null; p90: number | null; n: number }
interface Item { id: number; url: string; source: string; title: string; summary: string | null; published_at: string | null; seen_at: string; extract?: Extract | null }
interface Article { status: string; error?: string | null; title?: string | null; date?: string | null; text?: string; words?: number;
  meta?: Record<string, unknown>; tables?: (string[][] | { caption: string; rows: string[][] })[]; images?: string[] }
interface Feed { source: string; url: string; custom?: boolean }
interface Settings {
  enabled: boolean; interval_s: number; sources: Record<string, boolean>; custom_feeds: Feed[];
  watchlist: string[]; muted: string[]; alerts: boolean; retention_days: number;
  extract: boolean; extract_sources: Record<string, boolean>; intervals?: Record<string, number>;
}
interface SourceRow {
  source: string; url: string; custom: boolean; enabled: boolean; health: string; last_ok: number | null;
  error: string | null; paused_until: number | null; items_in_feed: number; hour: number; day: number; total: number; last_item: string | null;
  lag?: Lag | null;
}
interface HostStat { host: string; done: number; failed: number; words_p50: number | null; last: number | null; error: string | null;
  req_1h: number; req_24h: number; not_modified: number; refused: number }
interface ExtractStats { handed: number; by_status: Record<string, number>; with_text: number; paused: { host: string; until: number; reason: string }[]; lag?: Lag;
  hosts?: HostStat[]; requests?: { "1h": number; "24h": number; refused_24h: number } }
interface Status { settings: Settings; sources: SourceRow[]; known_sources: string[]; extract?: ExtractStats; lag?: Lag & { fastest: string | null }; collector: { polls: number; last_poll_at: number | null; last_poll_ms: number | null } }
interface Prefs { notify: boolean; sound: boolean; compact: boolean; summaries: boolean; window: number }

const WINDOWS: { v: number; l: string }[] = [{ v: 1, l: "1h" }, { v: 6, l: "6h" }, { v: 24, l: "24h" }, { v: 168, l: "7d" }, { v: 0, l: "All" }];
const PREFS_KEY = "news:prefs";
const DEFAULT_PREFS: Prefs = { notify: false, sound: false, compact: false, summaries: true, window: 24 };

function loadPrefs(): Prefs {
  try { return { ...DEFAULT_PREFS, ...JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") }; } catch { return DEFAULT_PREFS; }
}
const when = (i: Item) => Date.parse(i.published_at || i.seen_at);
function ago(ms: number): string {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  return s < 60 ? `${s | 0}s` : s < 3600 ? `${(s / 60) | 0}m` : s < 86400 ? `${(s / 3600) | 0}h` : `${(s / 86400) | 0}d`;
}
const ist = (ms: number) => new Date(ms).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
/** 42 -> "42s", 300 -> "5m", 7200 -> "2h" */
function dur(sec: number | null | undefined): string {
  if (sec == null) return "–";
  return sec < 60 ? `${Math.round(sec)}s` : sec < 3600 ? `${Math.round(sec / 60)}m` : `${(sec / 3600).toFixed(1)}h`;
}
/** Detection lag of one headline: published -> first seen. null when the feed gives no time or a future one, or
 *  when it came in its source's first poll (already in the feed: that says when monitoring started, not our speed). */
function lagOf(i: Item, firstSeen: Map<string, number>): number | null {
  if (!i.published_at || Date.parse(i.seen_at) <= (firstSeen.get(i.source) ?? 0) + 120_000) return null;
  const d = (Date.parse(i.seen_at) - Date.parse(i.published_at)) / 1000;
  return d >= 0 && d < 86400 ? d : null;
}
const lagClass = (d: number) => (d <= 120 ? "fast" : d <= 600 ? "mid" : "slow");
const STOP = new Set(("the a an and or of to in on for with by at from as is are was were be been it its this that these those after over "
  + "into up down out new news says said will can may vs amid how why what who when where his her their our your all more most than "
  + "about ahead stock stocks share shares market markets india indian today live check updates update price target top day week "
  + "year sees set gets get make makes just also not no yes per cent rs crore lakh ltd limited should could would quote "
  + "visit here there watch know first last big off look").split(" "));
const esc = (w: string) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Title with watchlist words wrapped in <mark> — built as React nodes, never innerHTML (headlines are untrusted). */
function Highlight({ text, words }: { text: string; words: string[] }) {
  if (!words.length) return <>{text}</>;
  const re = new RegExp(`(${words.map(esc).join("|")})`, "gi");
  return <>{text.split(re).map((part, i) => (i % 2 ? <mark key={i} className="news-mark">{part}</mark> : part))}</>;
}

function beep() {
  try {
    const ctx = new AudioContext();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = 880; g.gain.value = 0.06;
    o.connect(g).connect(ctx.destination); o.start(); o.stop(ctx.currentTime + 0.15);
  } catch { /* audio blocked */ }
}

export default function News() {
  const [items, setItems] = useState<Item[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [watchOnly, setWatchOnly] = useState(false);
  const [frozen, setFrozen] = useState(false);
  const [pending, setPending] = useState<Item[]>([]);
  const [prefs, setPrefsState] = useState<Prefs>(loadPrefs);
  const [showSettings, setShowSettings] = useState(false);
  const [fresh, setFresh] = useState<Set<number>>(new Set());
  const [deep, setDeep] = useState(false);
  const [deepItems, setDeepItems] = useState<Item[] | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const maxId = useRef(0);
  const frozenRef = useRef(false);
  frozenRef.current = frozen;
  const itemsRef = useRef<Item[]>([]);
  itemsRef.current = items;

  const setPrefs = (p: Partial<Prefs>) => setPrefsState((cur) => {
    const next = { ...cur, ...p };
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(next)); } catch { /* storage off */ }
    return next;
  });

  const settings = status?.settings;
  const watch = settings?.watchlist ?? [];
  const muted = settings?.muted ?? [];
  const watchRe = useMemo(() => (watch.length ? new RegExp(watch.map(esc).join("|"), "i") : null), [watch]);

  const notifyNew = useCallback((arrived: Item[]) => {
    if (!watchRe) return;
    const hits = arrived.filter((i) => watchRe.test(`${i.title} ${i.summary ?? ""}`));
    if (!hits.length) return;
    if (prefs.sound) beep();
    if (prefs.notify && "Notification" in window && Notification.permission === "granted") {
      for (const h of hits.slice(0, 3)) new Notification(`${h.source}: watchlist`, { body: h.title, tag: h.url });
    }
  }, [watchRe, prefs.sound, prefs.notify]);

  const load = useCallback(async (first = false) => {
    try {
      const [res, st] = await Promise.all([
        api.get<{ items: Item[]; max_id: number }>(first ? "/api/news/items?hours=2160&limit=2000" : `/api/news/items?since_id=${maxId.current}&limit=500`),
        api.get<Status>("/api/news/status"),
      ]);
      setStatus(st);
      setError(null);
      if (first) {
        setItems(res.items);
      } else if (res.items.length) {
        if (maxId.current > 0) {           // the first batch onto an empty page is backlog: no NEW badge, no alert
          notifyNew(res.items);
          setFresh((f) => new Set([...f, ...res.items.map((i) => i.id)]));
        }
        if (frozenRef.current) setPending((p) => [...res.items, ...p]);
        else setItems((cur) => [...res.items, ...cur].slice(0, 4000));
      }
      maxId.current = Math.max(maxId.current, res.max_id);
      if (!first && st.settings.extract) {   // the scraper finishes articles after their headline arrived: ask again
        const waiting = itemsRef.current.filter((i) => !i.extract || i.extract.status === "queued" || i.extract.status === "deferred")
          .slice(0, 200).map((i) => i.id);
        if (waiting.length) {
          const upd = await api.get<{ items: Item[] }>(`/api/news/items?limit=300&ids=${waiting.join(",")}`);
          const byId = new Map(upd.items.map((i) => [i.id, i.extract]));
          setItems((cur) => cur.map((i) => (byId.has(i.id) ? { ...i, extract: byId.get(i.id) } : i)));
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [notifyNew]);

  useEffect(() => { void load(true); }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {                         // live: the server pushes "news" after every poll and extraction batch
    const onNews = (e: Event) => {
      const p = (e as CustomEvent).detail as { new?: number; extracted?: number; polls?: number; last_poll_at?: number; last_poll_ms?: number };
      if (p.new || p.extracted) { void load(); return; }
      if (p.last_poll_at) setStatus((cur) => (cur ? { ...cur, collector: { polls: p.polls ?? cur.collector.polls, last_poll_at: p.last_poll_at!, last_poll_ms: p.last_poll_ms ?? null } } : cur));
    };
    const onOpen = () => void load();       // socket back: pull what it missed
    window.addEventListener("news:event", onNews);
    window.addEventListener("ws:open", onOpen);
    const t = setInterval(() => void load(), 60_000);   // fallback while the socket is down
    return () => { window.removeEventListener("news:event", onNews); window.removeEventListener("ws:open", onOpen); clearInterval(t); };
  }, [load]);
  const [, setTick] = useState(0);          // "12s ago" labels count up live
  useEffect(() => { const t = setInterval(() => setTick((n) => n + 1), 1000); return () => clearInterval(t); }, []);

  useEffect(() => {                         // "in article text": the server searches the scraper's stored text
    if (!deep || !q.trim()) { setDeepItems(null); return; }
    const t = setTimeout(() => {
      const hours = prefs.window ? `&hours=${prefs.window}` : "";
      api.get<{ items: Item[] }>(`/api/news/items?deep=true&limit=500&q=${encodeURIComponent(q.trim())}${hours}`)
        .then((r) => setDeepItems(r.items)).catch(() => setDeepItems([]));
    }, 400);
    return () => clearTimeout(t);
  }, [deep, q, prefs.window]);

  const unfreeze = () => { setItems((cur) => [...pending, ...cur].slice(0, 4000)); setPending([]); setFrozen(false); };

  const shown = useMemo(() => {
    const cut = prefs.window ? Date.now() - prefs.window * 3600_000 : 0;
    const ql = q.trim().toLowerCase();
    const mutedRe = muted.length ? new RegExp(muted.map(esc).join("|"), "i") : null;
    const seen = new Set<string>();
    return (deepItems ?? items)
      .filter((i) => (!cut || when(i) >= cut) && (!picked.size || picked.has(i.source))
        && (deepItems !== null || !ql || `${i.title} ${i.summary ?? ""}`.toLowerCase().includes(ql))
        && !(mutedRe && mutedRe.test(i.title))
        && (!watchOnly || (watchRe && watchRe.test(`${i.title} ${i.summary ?? ""}`))))
      .sort((a, b) => when(b) - when(a))
      .filter((i) => { const k = `${i.source}|${i.title}`; if (seen.has(k)) return false; seen.add(k); return true; })   // same story in two feeds
      .slice(0, 500);
  }, [items, deepItems, prefs.window, picked, q, muted, watchOnly, watchRe]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    const cut = prefs.window ? Date.now() - prefs.window * 3600_000 : 0;
    for (const i of items) if (!cut || when(i) >= cut) c[i.source] = (c[i.source] || 0) + 1;
    return c;
  }, [items, prefs.window]);

  const trending = useMemo(() => {          // words in the last hour's headlines, most frequent first
    const cut = Date.now() - 3600_000, c = new Map<string, number>();
    for (const i of items) {
      if (when(i) < cut) continue;
      const seen = new Set<string>();
      for (const w of i.title.match(/[A-Za-z][A-Za-z&.'-]{2,}/g) ?? []) {
        const k = w.toLowerCase().replace(/['.]+$/, "");
        if (STOP.has(k) || seen.has(k)) continue;
        seen.add(k); c.set(k, (c.get(k) || 0) + 1);
      }
    }
    return [...c.entries()].filter(([, n]) => n >= 2).sort((a, b) => b[1] - a[1]).slice(0, 14);
  }, [items]);
  const firstSeen = useMemo(() => {        // each source's first poll (its backlog): no lag chip
    const m = new Map<string, number>();
    for (const i of items) { const t = Date.parse(i.seen_at); if (!m.has(i.source) || t < m.get(i.source)!) m.set(i.source, t); }
    return m;
  }, [items]);
  const lastHour = useMemo(() => items.filter((i) => when(i) >= Date.now() - 3600_000).length, [items]);

  const saveSettings = async (s: Settings) => {
    const st = await api.put<Settings>("/api/news/settings", s);
    setStatus((cur) => (cur ? { ...cur, settings: st } : cur));
    void load();
  };

  const c = status?.collector;
  // one row per source (CNBC-TV18 has two feeds): worst health wins, errors joined
  const rank: Record<string, number> = { paused: 4, error: 3, waiting: 2, ok: 1, off: 0 };
  const bySource = useMemo(() => {
    const m = new Map<string, SourceRow>();
    for (const r of status?.sources ?? []) {
      const k = r.source + (r.custom ? " ★" : "");
      const cur = m.get(k);
      if (!cur) { m.set(k, { ...r }); continue; }
      if ((rank[r.health] ?? 0) > (rank[cur.health] ?? 0)) cur.health = r.health;
      cur.error = [cur.error, r.error].filter(Boolean).join("; ") || null;
      cur.paused_until = Math.max(cur.paused_until ?? 0, r.paused_until ?? 0) || null;
    }
    return [...m.entries()];
  }, [status]);   // eslint-disable-line react-hooks/exhaustive-deps
  const live = !!settings?.enabled;
  const togglePick = (s: string) => setPicked((p) => { const n = new Set(p); if (n.has(s)) n.delete(s); else n.add(s); return n; });

  return (
    <div data-testid="news-page">
      <div className="news-head">
        <h1 className="page-title" style={{ margin: 0 }}>News</h1>
        <span className={`news-live ${live ? "on" : ""}`} title={live ? "Monitoring" : "Monitoring is off"} />
        <span className="text-dim news-meta">
          {live ? <>every {settings!.interval_s}s · {c?.polls ?? 0} polls · last {c?.last_poll_at ? ago(c.last_poll_at * 1000) : "–"} ago{c?.last_poll_ms != null ? ` (${(c.last_poll_ms / 1000).toFixed(1)}s)` : ""}</> : "monitoring is off"}
          {" · monitoring only, never sent to AI analysis"}
        </span>
        <span style={{ flex: 1 }} />
        <button type="button" className="btn-sm" onClick={() => void api.post("/api/news/poll", {})} disabled={!live}>Poll now</button>
        <a className="btn-sm" href={`/api/news/export.csv?hours=${prefs.window || 2160}${q ? `&q=${encodeURIComponent(q)}` : ""}`}>Export CSV</a>
        <button type="button" className="btn-sm primary" onClick={() => setShowSettings(true)} data-testid="news-settings-btn">⚙ Settings</button>
      </div>

      {settings && !live && (
        <div className="news-banner">
          <span>News monitoring is off. Turn it on to poll {status!.sources.filter((s) => s.enabled).length} feeds every {settings.interval_s}s.</span>
          <Toggle on={false} onChange={() => void saveSettings({ ...settings, enabled: true })} label="Monitor news" data-testid="news-enable" />
        </div>
      )}
      {error && <div className="news-banner danger">Can't reach the server: {error}</div>}

      {live && status && (
        <div className="news-stats" data-testid="news-stats">
          <div className="news-stat"><span>Headlines · last hour</span><b>{lastHour}</b></div>
          <div className="news-stat" title="Published → first seen, median (90th percentile) over the last 24 h; each source's first poll is left out">
            <span>Detection lag</span><b>{dur(status.lag?.p50)}</b><small>p90 {dur(status.lag?.p90)} · n {status.lag?.n ?? 0}</small></div>
          <div className="news-stat" title="Lowest median detection lag (24 h, at least 3 stories)"><span>Fastest source</span><b>{status.lag?.fastest ?? "–"}</b></div>
          {settings?.extract && (
            <div className="news-stat" title="Headline seen → article text stored, median"><span>Full texts</span><b>{status.extract?.with_text ?? 0}</b>
              <small>text in {dur(status.extract?.lag?.p50)}</small></div>
          )}
          <div className="news-stat"><span>Sources up</span><b>{bySource.filter(([, s]) => s.health === "ok").length}/{bySource.filter(([, s]) => s.enabled).length}</b></div>
        </div>
      )}

      <div className="news-toolbar">
        <input type="search" placeholder="Search headlines…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search headlines" />
        <Toggle size="sm" on={deep} onChange={setDeep} label="In article text" disabled={!settings?.extract}
          title={settings?.extract ? "Also search the full text the scraper extracted" : "Turn on full text in Settings"} />
        {WINDOWS.map((w) => <button key={w.v} type="button" className={`chip${prefs.window === w.v ? " on" : ""}`} onClick={() => setPrefs({ window: w.v })}>{w.l}</button>)}
        <Toggle size="sm" on={watchOnly} onChange={setWatchOnly} label="Watchlist only" disabled={!watch.length} title={watch.length ? "" : "Add watchlist words in Settings"} />
        <Toggle size="sm" on={frozen} onChange={(v) => (v ? setFrozen(true) : unfreeze())} label="Pause list" />
        {pending.length > 0 && <button type="button" className="btn-sm primary" onClick={unfreeze}>{pending.length} new ↑</button>}
      </div>

      {trending.length > 0 && (
        <div className="news-chips" data-testid="news-trending">
          <span className="text-dim news-meta">Trending · 1h</span>
          {trending.map(([w, n]) => <button key={w} type="button" className={`chip${q.toLowerCase() === w ? " on" : ""}`} onClick={() => setQ(q.toLowerCase() === w ? "" : w)}>{w} <span className="mono">{n}</span></button>)}
        </div>
      )}
      <div className="news-chips">
        {Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([s, n]) => (
          <button key={s} type="button" className={`chip${picked.has(s) ? " on" : ""}`} onClick={() => togglePick(s)}>{s} <span className="mono">{n}</span></button>
        ))}
        {picked.size > 0 && <button type="button" className="chip" onClick={() => setPicked(new Set())}>clear</button>}
      </div>

      <div className="news-grid">
        <div style={{ minWidth: 0 }}>
        <div className="widget">
          <h3>Headlines <span className="mono" style={{ marginLeft: 8, color: "var(--text-faint)" }}>{shown.length}</span></h3>
          <div className="news-list">
            {shown.length === 0 && <p className="empty">{live ? "No headlines match." : "Turn monitoring on to start collecting."}</p>}
            {shown.map((i) => {
              const hit = !!watchRe && watchRe.test(`${i.title} ${i.summary ?? ""}`);
              return (
                <article key={i.id} className={`news-item${hit ? " hit" : ""}${fresh.has(i.id) ? " fresh" : ""}${prefs.compact ? " compact" : ""}`}>
                  <div className="news-when" title={ist(when(i)) + " IST"}>
                    <span>{ago(when(i))}{(() => { const d = lagOf(i, firstSeen); return d == null ? null : (
                      <span className={`news-lag ${lagClass(d)}`} title={`Published ${ist(Date.parse(i.published_at!))}, first seen ${ist(Date.parse(i.seen_at))} IST`}> +{dur(d)}</span>); })()}</span>
                    <span className="badge neutral">{i.source}</span>
                  </div>
                  <div style={{ minWidth: 0 }}>
                    {/^https?:\/\//.test(i.url)
                      ? <a href={i.url} target="_blank" rel="noopener noreferrer" className="news-title"><Highlight text={i.title} words={watch} /></a>
                      : <span className="news-title"><Highlight text={i.title} words={watch} /></span>}
                    {fresh.has(i.id) && <span className="badge-new">NEW</span>}
                    <TextBadge e={i.extract} open={open === i.id}
                      on={!!settings?.extract && (settings.extract_sources[i.source] ?? true) && Date.now() - Date.parse(i.seen_at) < 12 * 3600_000}
                      onClick={() => setOpen(open === i.id ? null : i.id)} />
                    {prefs.summaries && !prefs.compact && i.summary && <div className="news-sum"><Highlight text={i.summary} words={watch} /></div>}
                    {open === i.id && <Reader url={i.url} words={watch} />}
                  </div>
                </article>
              );
            })}
          </div>
        </div>
          {settings?.extract && status?.extract && (
            <div className="widget" style={{ marginTop: 12 }} data-testid="news-extract">
              <h3>Full text · your scraper</h3>
              <div className="widget-body">
                <ScraperInsights x={status.extract} />
                <div className="news-src"><span className="news-dot ok" /><span>articles with text</span><span className="mono">{status.extract.with_text}</span></div>
                {Object.entries(status.extract.by_status).filter(([k]) => k !== "done").map(([k, n]) => (
                  <div key={k} className="news-src">
                    <span className={`news-dot ${k === "queued" || k === "deferred" ? "waiting" : "error"}`} /><span>{k}</span><span className="mono">{n}</span>
                  </div>
                ))}
                {status.extract.paused.map((p) => <div key={p.host} className="news-src-err">{p.host} paused until {ist(p.until * 1000)}: {p.reason}</div>)}
              </div>
            </div>
          )}
        </div>

        <div className="news-side">
          <div className="widget">
            <h3>Sources</h3>
            <div className="widget-body">
              {bySource.map(([name, s]) => (
                <div key={name} className="news-src" title={s.health}>
                  <span className={`news-dot ${s.health}`} />
                  <span className="news-src-name">{name}</span>
                  <span className="mono text-dim" title={s.lag?.n ? `detection lag p50 ${dur(s.lag.p50)}, p90 ${dur(s.lag.p90)} (n ${s.lag.n})` : ""}>
                    {s.hour}/h · {s.day}/24h{s.lag?.n ? ` · ${dur(s.lag.p50)}` : ""}</span>
                  {(s.error || s.paused_until) && <div className="news-src-err">{s.error}{s.paused_until ? ` until ${ist(s.paused_until * 1000)}` : ""}</div>}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {showSettings && settings && status && (
        <SettingsModal initial={settings} status={status} prefs={prefs} setPrefs={setPrefs}
          onSave={async (s) => { await saveSettings(s); setShowSettings(false); }} onClose={() => setShowSettings(false)}
          onCleared={() => { setItems([]); maxId.current = 0; }} />
      )}
    </div>
  );
}

/** What the scraper did in the last 24 h, per site: is it worth fetching (ok rate, words) and are we too much
 *  for it (requests, 304 share, refusals)? */
function ScraperInsights({ x }: { x: ExtractStats }) {
  const r = x.requests, hosts = x.hosts ?? [];
  const tot = hosts.reduce((a, h) => ({ done: a.done + h.done, failed: a.failed + h.failed }), { done: 0, failed: 0 });
  const rate = (d: number, f: number) => (d + f ? `${Math.round((100 * d) / (d + f))}%` : "–");
  return (
    <div data-testid="news-insights">
      <div className="news-stats news-stats-sm">
        <div className="news-stat" title="Articles stored with text / all finished, last 24 h"><span>Success</span><b>{rate(tot.done, tot.failed)}</b><small>{tot.done} ok · {tot.failed} not</small></div>
        <div className="news-stat" title="Headline seen → article text stored"><span>Text in</span><b>{dur(x.lag?.p50)}</b><small>p90 {dur(x.lag?.p90)}</small></div>
        <div className="news-stat" title="Requests the scraper sent to publishers (pages, robots.txt, retries)"><span>Requests</span><b>{r?.["1h"] ?? 0}/h</b><small>{r?.["24h"] ?? 0} in 24h</small></div>
        <div className="news-stat" title="401/403/429 answers in 24 h: a site saying no or slow down"><span>Refused</span><b className={r?.refused_24h ? "neg" : ""}>{r?.refused_24h ?? 0}</b><small>24h</small></div>
      </div>
      {hosts.length > 0 && (
        <table className="news-table news-hosts"><thead><tr><th>Site</th><th title="Stored with text / finished">OK</th><th title="Median words per article">Words</th>
          <th title="Requests in 24 h (last hour)">Req</th><th title="Answered 304 Not Modified">304</th><th title="401/403/429">No</th><th>Last</th></tr></thead>
          <tbody>{hosts.map((h) => (
            <tr key={h.host} title={h.error ? `last problem: ${h.error}` : ""}>
              <td className="news-url">{h.host.replace(/^www\./, "")}</td><td className="mono">{rate(h.done, h.failed)}</td>
              <td className="mono">{h.words_p50 ?? "–"}</td><td className="mono">{h.req_24h} ({h.req_1h})</td>
              <td className="mono">{h.req_24h ? `${Math.round((100 * h.not_modified) / h.req_24h)}%` : "–"}</td>
              <td className={`mono${h.refused ? " neg" : ""}`}>{h.refused}</td><td className="mono">{h.last ? ago(h.last * 1000) : "–"}</td>
            </tr>))}</tbody></table>
      )}
    </div>
  );
}

function TextBadge({ e, on, open, onClick }: { e?: Extract | null; on: boolean; open: boolean; onClick: () => void }) {
  if (e?.status === "done" && e.chars > 0)
    return <button type="button" className={`news-tbadge ok${open ? " open" : ""}`} onClick={onClick} title="Read the extracted article">{open ? "▾" : "▸"} TEXT</button>;
  if (e && e.status && !["queued", "deferred"].includes(e.status))
    return <span className="news-tbadge off" title={e.error || e.status}>{e.status}</span>;
  // the scraper works through the last 12 h of headlines; older ones are not fetched, so they get no badge
  return on ? <span className="news-tbadge wait" title="Waiting for the scraper">…</span> : null;
}

function Reader({ url, words }: { url: string; words: string[] }) {
  const [a, setA] = useState<Article | null>(null);
  useEffect(() => {
    api.get<Article>(`/api/news/article?url=${encodeURIComponent(url)}`).then(setA).catch(() => setA({ status: "error" }));
  }, [url]);
  if (!a) return <div className="news-reader text-dim">Loading…</div>;
  if (a.status !== "done") return <div className="news-reader text-dim">No text: {a.error || a.status}</div>;
  const m = a.meta ?? {};
  const tags = ([] as unknown[]).concat((m.tags as unknown[]) ?? [], (m.categories as unknown[]) ?? [])
    .filter((t): t is string => typeof t === "string").slice(0, 8);
  const meta = [m.author, m.sitename, a.date ? ist(Date.parse(a.date)) : null, `${a.words} words · ~${Math.max(1, Math.round((a.words ?? 0) / 200))} min read`]
    .filter(Boolean).join(" · ");
  const images = (a.images ?? []).filter((u) => /^https?:\/\//.test(u)).slice(0, 6);
  return (
    <div className="news-reader" data-testid="news-reader">
      <div className="news-reader-meta">{meta}</div>
      {tags.length > 0 && <div className="news-words">{tags.map((t) => <span key={t} className="chip">{t}</span>)}</div>}
      <div className="news-reader-text"><Highlight text={a.text ?? ""} words={words} /></div>
      {(a.tables ?? []).slice(0, 2).map((t, k) => {
        const rows = Array.isArray(t) ? t : t.rows;
        return (
          <table key={k} className="news-table"><tbody>
            {rows.slice(0, 12).map((r, ri) => <tr key={ri}>{r.map((c, ci) => <td key={ci}>{c}</td>)}</tr>)}
          </tbody></table>
        );
      })}
      {images.length > 0 && (
        <div className="news-reader-meta">Images:{images.map((u, k) => <a key={u} href={u} target="_blank" rel="noopener noreferrer"> {k + 1}</a>)}</div>
      )}
    </div>
  );
}

/** A source's own poll interval; empty = the default (Monitoring > Poll every). */
function Every({ src, s, set }: { src: string; s: Settings; set: (p: Partial<Settings>) => void }) {
  const v = s.intervals?.[src];
  return (
    <label className="cform-check" title="Poll this source every N seconds (10-3600). Empty = the default interval.">
      every <input className="cform-num" type="number" min={10} max={3600} style={{ width: 64 }} value={v ?? ""} placeholder={String(s.interval_s)}
        aria-label={`${src} poll interval`} onChange={(e) => {
          const n = { ...(s.intervals ?? {}) };
          if (e.target.value === "") delete n[src]; else n[src] = Math.round(Number(e.target.value));
          set({ intervals: n });
        }} /> s
    </label>
  );
}

function Words({ words, onChange, placeholder }: { words: string[]; onChange: (w: string[]) => void; placeholder: string }) {
  const [txt, setTxt] = useState("");
  const add = () => {
    const w = txt.trim();
    if (w && !words.some((x) => x.toLowerCase() === w.toLowerCase())) onChange([...words, w]);
    setTxt("");
  };
  return (
    <div>
      <div className="news-words">
        {words.map((w) => <span key={w} className="chip on">{w} <button type="button" className="news-x" onClick={() => onChange(words.filter((x) => x !== w))} aria-label={`Remove ${w}`}>×</button></span>)}
      </div>
      <input value={txt} placeholder={placeholder} onChange={(e) => setTxt(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} maxLength={60} />
    </div>
  );
}

function SettingsModal({ initial, status, prefs, setPrefs, onSave, onClose, onCleared }: {
  initial: Settings; status: Status; prefs: Prefs; setPrefs: (p: Partial<Prefs>) => void;
  onSave: (s: Settings) => Promise<void>; onClose: () => void; onCleared: () => void;
}) {
  const [s, setS] = useState<Settings>(initial);
  const [tab, setTab] = useState("monitor");
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [feed, setFeed] = useState({ source: "", url: "" });
  const [test, setTest] = useState<{ ok: boolean; count?: number; sample?: string[]; error?: string } | null>(null);
  const set = (p: Partial<Settings>) => setS((cur) => ({ ...cur, ...p }));
  const health = (src: string) => status.sources.find((x) => x.source === src);

  const save = async () => {
    setSaving(true); setErr(null);
    try { await onSave(s); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setSaving(false); }
  };
  const runTest = async () => {
    setTest(null);
    try { setTest(await api.post("/api/news/test-feed", { url: feed.url })); } catch (e) { setTest({ ok: false, error: e instanceof Error ? e.message : String(e) }); }
  };
  const tabs = [{ id: "monitor", label: "Monitoring" }, { id: "sources", label: "Sources" }, { id: "watch", label: "Watchlist & alerts" },
    { id: "browser", label: "This browser" }, { id: "data", label: "Data" }];
  let body: ReactNode = null;

  if (tab === "monitor") body = (
    <Section title="Collection">
      <Row label="Monitor news" hint="Off = nothing is polled."><Toggle on={s.enabled} onChange={(v) => set({ enabled: v })} /></Row>
      <Row label="Poll every (seconds)" hint="10-600. The default for every source; Sources sets one per source. Feeds that publish their own refresh time (Moneycontrol: 30 s) are never polled faster than that."><Num value={s.interval_s} min={10} max={600} onChange={(v) => set({ interval_s: v })} ariaLabel="Poll interval" /></Row>
      <Row label="Fetch full article text" hint="Your scraper (scraper/scrape.py) opens each new headline's article: text, author, tags, tables, PDF text, image links. Stored in data/scrape.db on the server.">
        <Toggle on={s.extract} onChange={(v) => set({ extract: v })} data-testid="news-extract-toggle" />
      </Row>
      <Row label="Keep headlines (days)" hint="Older headlines are deleted hourly."><Num value={s.retention_days} min={1} max={90} onChange={(v) => set({ retention_days: v })} ariaLabel="Retention days" /></Row>
      <p className="field-hint">Headlines only (title, link, time, summary), honest user agent, conditional requests. A source that refuses (403/429) or asks for a pause is left alone for that long. Nothing here reaches the analyzer or the AI.</p>
    </Section>
  );
  if (tab === "sources") body = (
    <>
      <Section title="Built-in sources">
        {status.known_sources.map((src) => {
          const h = health(src);
          return (
            <Check key={src} label={<>{src} {h && <span className={`news-dot ${h.health}`} style={{ marginLeft: 6 }} />}</>} checked={s.sources[src] ?? true}
              onChange={(v) => set({ sources: { ...s.sources, [src]: v } })}>
              <label className="cform-check" title="Fetch this source's articles with the scraper (Monitoring > Fetch full article text)">
                <input type="checkbox" checked={s.extract_sources[src] ?? false}
                  onChange={(e) => set({ extract_sources: { ...s.extract_sources, [src]: e.target.checked } })} /> full text
              </label>
              <Every src={src} s={s} set={set} />
              {h && <span className="mono text-dim">{h.day}/24h{h.error ? ` · ${h.error}` : ""}</span>}
            </Check>
          );
        })}
      </Section>
      <Section title="Your own feeds">
        {s.custom_feeds.map((f) => (
          <Row key={f.url} label={f.source}><span className="mono text-dim news-url">{f.url}</span><Every src={f.source} s={s} set={set} />
            <button type="button" className="btn-sm danger" onClick={() => set({ custom_feeds: s.custom_feeds.filter((x) => x.url !== f.url) })}>Remove</button></Row>
        ))}
        <div className="news-addfeed">
          <input placeholder="Name (e.g. Business Today)" value={feed.source} maxLength={40} onChange={(e) => setFeed({ ...feed, source: e.target.value })} />
          <input placeholder="https://…/rss.xml" value={feed.url} maxLength={500} onChange={(e) => { setFeed({ ...feed, url: e.target.value }); setTest(null); }} />
          <button type="button" className="btn-sm" onClick={runTest} disabled={!feed.url}>Test</button>
          <button type="button" className="btn-sm primary" disabled={!test?.ok || !feed.source || s.custom_feeds.length >= 20}
            onClick={() => { set({ custom_feeds: [...s.custom_feeds, { source: feed.source.trim(), url: feed.url.trim() }] }); setFeed({ source: "", url: "" }); setTest(null); }}>Add</button>
        </div>
        {test && <p className="field-hint" style={{ color: test.ok ? "var(--green)" : "var(--red)" }}>{test.ok ? `OK: ${test.count} headlines, e.g. "${test.sample?.[0]}"` : test.error}</p>}
        <p className="field-hint">Any public RSS, Atom or Google-news sitemap. Test it first; only feeds that return headlines can be added. Check the site's terms before adding it.</p>
      </Section>
    </>
  );
  if (tab === "watch") body = (
    <>
      <Section title="Watchlist">
        <Words words={s.watchlist} onChange={(w) => set({ watchlist: w })} placeholder="Add a word or company and press Enter (e.g. Reliance, RBI, results)" />
        <p className="field-hint">Matching headlines are highlighted and can be filtered with "Watchlist only".</p>
      </Section>
      <Section title="Muted words">
        <Words words={s.muted} onChange={(w) => set({ muted: w })} placeholder="Hide headlines containing… (e.g. horoscope, cricket)" />
      </Section>
      <Section title="Alerts">
        <Check label="Send watchlist hits to my notification channels" checked={s.alerts} onChange={(v) => set({ alerts: v })} />
        <p className="field-hint">On the Notifications page, tick "News watchlist hits" on the channels that should get them (Telegram, Discord, email, webhook). Stories already in a feed when monitoring starts are never alerted.</p>
      </Section>
    </>
  );
  if (tab === "browser") body = (
    <Section title="This browser only">
      <Check label="Desktop notification on a watchlist hit" checked={prefs.notify} onChange={(v) => {
        if (v && "Notification" in window && Notification.permission !== "granted") void Notification.requestPermission();
        setPrefs({ notify: v });
      }} />
      <Check label="Play a sound on a watchlist hit" checked={prefs.sound} onChange={(v) => setPrefs({ sound: v })} />
      <Check label="Compact list (titles only)" checked={prefs.compact} onChange={(v) => setPrefs({ compact: v })} />
      <Check label="Show summaries" checked={prefs.summaries} onChange={(v) => setPrefs({ summaries: v })} />
      <Row label="Default time window"><Sel value={prefs.window} options={WINDOWS} onChange={(v) => setPrefs({ window: v })} ariaLabel="Default time window" /></Row>
      <p className="field-hint">Saved in this browser at once; the Save button is for the server settings.</p>
    </Section>
  );
  if (tab === "data") body = (
    <Section title="Stored headlines">
      <Row label="Export"><a className="btn-sm" href="/api/news/export.csv?hours=2160">Download CSV (all kept)</a></Row>
      <Row label="Clear" hint="Deletes every stored headline; monitoring keeps collecting.">
        <button type="button" className="btn-sm danger" onClick={async () => {
          if (!window.confirm("Delete every stored headline?")) return;
          await api.delete("/api/news/items"); onCleared();
        }}>Delete all headlines</button>
      </Row>
    </Section>
  );

  return (
    <Modal title="News settings" onClose={onClose} width={720} tabs={tabs} tab={tab} onTab={setTab} testid="news-settings"
      footer={<>{err && <span style={{ color: "var(--red)", marginRight: "auto" }}>{err}</span>}
        <button type="button" className="btn-sm" onClick={onClose}>Cancel</button>
        <button type="button" className="btn-sm primary" onClick={save} disabled={saving} data-testid="news-save">{saving ? "Saving…" : "Save"}</button></>}>
      {body}
    </Modal>
  );
}
