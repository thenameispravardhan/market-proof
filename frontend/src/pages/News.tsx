// News page: a live headline monitor (Moneycontrol, NDTV, Zee Business, CNBC-TV18, ET, Mint, BusinessLine, BSE
// + your own feeds). Monitoring only: nothing here is sent to the analyzer or the AI.
// Server-side settings (what is collected, how often, alerts) live in /api/news/settings; per-browser
// conveniences (sound, desktop notifications, view) live in localStorage.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { Toggle } from "../components/common/Toggle";
import { Check, Modal, Num, Row, Section, Sel } from "../components/trade/chartUi";

interface Item { id: number; url: string; source: string; title: string; summary: string | null; published_at: string | null; seen_at: string }
interface Feed { source: string; url: string; custom?: boolean }
interface Settings {
  enabled: boolean; interval_s: number; sources: Record<string, boolean>; custom_feeds: Feed[];
  watchlist: string[]; muted: string[]; alerts: boolean; retention_days: number;
}
interface SourceRow {
  source: string; url: string; custom: boolean; enabled: boolean; health: string; last_ok: number | null;
  error: string | null; paused_until: number | null; items_in_feed: number; hour: number; day: number; total: number; last_item: string | null;
}
interface Status { settings: Settings; sources: SourceRow[]; known_sources: string[]; collector: { polls: number; last_poll_at: number | null; last_poll_ms: number | null } }
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
  const maxId = useRef(0);
  const frozenRef = useRef(false);
  frozenRef.current = frozen;

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
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [notifyNew]);

  useEffect(() => { void load(true); }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  const every = Math.max(10, settings?.interval_s ?? 20) * 1000;
  useEffect(() => {
    const t = setInterval(() => void load(), every);
    return () => clearInterval(t);
  }, [load, every]);

  const unfreeze = () => { setItems((cur) => [...pending, ...cur].slice(0, 4000)); setPending([]); setFrozen(false); };

  const shown = useMemo(() => {
    const cut = prefs.window ? Date.now() - prefs.window * 3600_000 : 0;
    const ql = q.trim().toLowerCase();
    const mutedRe = muted.length ? new RegExp(muted.map(esc).join("|"), "i") : null;
    const seen = new Set<string>();
    return items
      .filter((i) => (!cut || when(i) >= cut) && (!picked.size || picked.has(i.source))
        && (!ql || `${i.title} ${i.summary ?? ""}`.toLowerCase().includes(ql))
        && !(mutedRe && mutedRe.test(i.title))
        && (!watchOnly || (watchRe && watchRe.test(`${i.title} ${i.summary ?? ""}`))))
      .sort((a, b) => when(b) - when(a))
      .filter((i) => { const k = `${i.source}|${i.title}`; if (seen.has(k)) return false; seen.add(k); return true; })   // same story in two feeds
      .slice(0, 500);
  }, [items, prefs.window, picked, q, muted, watchOnly, watchRe]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    const cut = prefs.window ? Date.now() - prefs.window * 3600_000 : 0;
    for (const i of items) if (!cut || when(i) >= cut) c[i.source] = (c[i.source] || 0) + 1;
    return c;
  }, [items, prefs.window]);

  const hist = useMemo(() => {
    const bins = new Array(24).fill(0), now = Date.now();
    for (const i of items) { const h = Math.floor((now - when(i)) / 3600_000); if (h >= 0 && h < 24) bins[23 - h]++; }
    return bins;
  }, [items]);

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
        <button type="button" className="btn-sm" onClick={() => { void api.post("/api/news/poll", {}); setTimeout(() => void load(), 3000); }} disabled={!live}>Poll now</button>
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

      <div className="news-toolbar">
        <input type="search" placeholder="Search headlines…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search headlines" />
        {WINDOWS.map((w) => <button key={w.v} type="button" className={`chip${prefs.window === w.v ? " on" : ""}`} onClick={() => setPrefs({ window: w.v })}>{w.l}</button>)}
        <Toggle size="sm" on={watchOnly} onChange={setWatchOnly} label="Watchlist only" disabled={!watch.length} title={watch.length ? "" : "Add watchlist words in Settings"} />
        <Toggle size="sm" on={frozen} onChange={(v) => (v ? setFrozen(true) : unfreeze())} label="Pause list" />
        {pending.length > 0 && <button type="button" className="btn-sm primary" onClick={unfreeze}>{pending.length} new ↑</button>}
      </div>

      <div className="news-chips">
        {Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([s, n]) => (
          <button key={s} type="button" className={`chip${picked.has(s) ? " on" : ""}`} onClick={() => togglePick(s)}>{s} <span className="mono">{n}</span></button>
        ))}
        {picked.size > 0 && <button type="button" className="chip" onClick={() => setPicked(new Set())}>clear</button>}
      </div>

      <div className="news-grid">
        <div className="widget">
          <h3>Headlines <span className="mono" style={{ marginLeft: 8, color: "var(--text-faint)" }}>{shown.length}</span></h3>
          <div className="news-list">
            {shown.length === 0 && <p className="empty">{live ? "No headlines match." : "Turn monitoring on to start collecting."}</p>}
            {shown.map((i) => {
              const hit = !!watchRe && watchRe.test(`${i.title} ${i.summary ?? ""}`);
              return (
                <article key={i.id} className={`news-item${hit ? " hit" : ""}${fresh.has(i.id) ? " fresh" : ""}${prefs.compact ? " compact" : ""}`}>
                  <div className="news-when" title={ist(when(i)) + " IST"}>{ago(when(i))}<span className="badge neutral">{i.source}</span></div>
                  <div style={{ minWidth: 0 }}>
                    {/^https?:\/\//.test(i.url)
                      ? <a href={i.url} target="_blank" rel="noopener noreferrer" className="news-title"><Highlight text={i.title} words={watch} /></a>
                      : <span className="news-title"><Highlight text={i.title} words={watch} /></span>}
                    {fresh.has(i.id) && <span className="badge-new">NEW</span>}
                    {prefs.summaries && !prefs.compact && i.summary && <div className="news-sum"><Highlight text={i.summary} words={watch} /></div>}
                  </div>
                </article>
              );
            })}
          </div>
        </div>

        <div className="news-side">
          <div className="widget">
            <h3>Sources</h3>
            <div className="widget-body">
              {bySource.map(([name, s]) => (
                <div key={name} className="news-src" title={s.health}>
                  <span className={`news-dot ${s.health}`} />
                  <span className="news-src-name">{name}</span>
                  <span className="mono text-dim">{s.hour}/h · {s.day}/24h</span>
                  {(s.error || s.paused_until) && <div className="news-src-err">{s.error}{s.paused_until ? ` until ${ist(s.paused_until * 1000)}` : ""}</div>}
                </div>
              ))}
            </div>
          </div>
          <div className="widget" style={{ marginTop: 12 }}>
            <h3>Headlines per hour · 24h</h3>
            <div className="widget-body">
              <div className="news-hist">{hist.map((n, k) => <div key={k} style={{ height: `${(100 * n) / Math.max(1, ...hist)}%` }} title={`${23 - k}h ago: ${n}`} />)}</div>
              <div className="news-axis"><span>-24h</span><span>-12h</span><span>now</span></div>
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
      <Row label="Poll every (seconds)" hint="10-600. Feeds that publish their own refresh time (Moneycontrol: 30 s) are never polled faster than that."><Num value={s.interval_s} min={10} max={600} onChange={(v) => set({ interval_s: v })} ariaLabel="Poll interval" /></Row>
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
              {h && <span className="mono text-dim">{h.day}/24h{h.error ? ` · ${h.error}` : ""}</span>}
            </Check>
          );
        })}
      </Section>
      <Section title="Your own feeds">
        {s.custom_feeds.map((f) => (
          <Row key={f.url} label={f.source}><span className="mono text-dim news-url">{f.url}</span>
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
