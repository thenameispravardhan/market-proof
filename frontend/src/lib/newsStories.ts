// News page: the same story from several outlets, grouped. Keyword overlap on the titles plus the stocks the
// server tagged, never a model (the page is monitoring only). Conservative: a story wrongly merged would hide a
// headline, so two headlines join only when they share 3+ words besides the stock, most of the shorter one's
// weight, and no stock sets them apart ("TCS Q2 profit up 5%" and "Infosys Q2 profit up 5%" stay two stories).
// Weights: a tagged stock 3, a code or a specific number ("q2", "19323") 2, a word 1.

export const STOP = new Set(("the a an and or of to in on for with by at from as is are was were be been it its this that these those after over "
  + "into up down out new news says said will can may vs amid how why what who when where his her their our your all more most than "
  + "about ahead stock stocks share shares market markets india indian today live check updates update price target top day week "
  + "year sees set gets get make makes just also not no yes per cent rs crore lakh ltd limited should could would quote "
  + "visit here there watch know first last big off look").split(" "));

const WINDOW_MS = 6 * 3600_000;   // a story's reports come within 6 h of the first

export interface StoryItem { id: number; title: string; t: number; tickers?: string[] }

/** "NSE:BAJAJ-AUTO-EQ" -> "BAJAJ-AUTO", "BSE:ACME-A" -> "ACME". */
export const tickerName = (sym: string) => (sym.split(":").pop() ?? sym).replace(/-[A-Z]{1,2}$/, "");

/** What a headline is about: words without stop words, lightly stemmed ("rises" = "rise"), numbers without commas
 *  ("₹19,323 crore" -> "19323"), codes kept whole ("q2", "fy27"), and each tagged stock ("$NSE:RELIANCE-EQ"). */
export function storyTokens(title: string, tickers: string[] = []): Set<string> {
  const out = new Set<string>(tickers.map((t) => `$${t}`));
  for (const raw of title.toLowerCase().match(/[a-z0-9][a-z0-9&'’.,]*/g) ?? []) {
    let w = raw.replace(/['’]s$/, "").replace(/[.,']+$/, "");
    if (/\d/.test(w)) { out.add(w.replace(/,/g, "")); continue; }
    w = w.replace(/[.,]/g, "");
    if (w.length < 3 || STOP.has(w)) continue;
    if (w.length > 5 && w.endsWith("ing")) w = w.slice(0, -3);
    else if (w.length > 4 && w.endsWith("ed")) w = w.slice(0, -2);
    else if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss")) w = w.slice(0, -1);
    out.add(w);
  }
  return out;
}

const weight = (k: string) => (k.startsWith("$") ? 3 : /\d/.test(k) && (k.length >= 3 || /[a-z]/.test(k)) ? 2 : 1);
const total = (toks: Set<string>) => { let w = 0; for (const k of toks) w += weight(k); return w; };

/** Each headline's story, as the id of the story's first report (its own id when it is the first). Headlines are
 *  taken oldest first and each is compared with the first report of every story begun in the 6 h before it. */
export function clusterStories(items: StoryItem[]): Map<number, number> {
  const sorted = [...items].sort((a, b) => a.t - b.t || a.id - b.id);
  const lead = new Map<number, number>();
  const stories: { id: number; t: number; tokens: Set<string>; w: number; tickers: Set<string> }[] = [];
  const index = new Map<string, { from: number; list: number[] }>();   // word -> stories using it, oldest first
  for (const it of sorted) {
    const toks = storyTokens(it.title, it.tickers), w = total(toks);
    const shared = new Map<number, { w: number; words: number }>();
    for (const k of toks) {
      const e = index.get(k);
      if (!e) continue;
      while (e.from < e.list.length && it.t - stories[e.list[e.from]].t > WINDOW_MS) e.from++;   // too old to join
      for (let p = e.from; p < e.list.length; p++) {
        const c = shared.get(e.list[p]) ?? { w: 0, words: 0 };
        c.w += weight(k);
        if (!k.startsWith("$")) c.words++;
        shared.set(e.list[p], c);
      }
    }
    let best = -1, bestScore = 0;
    for (const [s, c] of shared) {
      const S = stories[s];
      if (c.words < 3 || (it.tickers?.length && S.tickers.size && !it.tickers.some((x) => S.tickers.has(x)))) continue;
      const overlap = c.w / Math.min(w, S.w), jaccard = c.w / (w + S.w - c.w);
      if (overlap >= 0.6 && jaccard >= 0.3 && overlap > bestScore) { best = s; bestScore = overlap; }
    }
    if (best >= 0) { lead.set(it.id, stories[best].id); continue; }
    const s = stories.push({ id: it.id, t: it.t, tokens: toks, w, tickers: new Set(it.tickers ?? []) }) - 1;
    lead.set(it.id, it.id);
    for (const k of toks) {
      const e = index.get(k);
      if (e) e.list.push(s); else index.set(k, { from: 0, list: [s] });
    }
  }
  return lead;
}
