// usePanelSizes — remembers how tall the operator made each panel.
//
// The resizing itself is native: `.widget { resize: vertical }` in App.css
// gives every panel a real drag grip with no library and no pointer
// handlers. This hook only persists the result, because a resize that
// vanishes on refresh is worse than no resize at all.
//
// HEIGHT ONLY, deliberately. Panels are grid children
// (`grid-template-columns: 1fr 1fr` / `repeat(auto-fill, minmax(380px,1fr))`)
// so the track owns the width — an inline width is overridden the moment it
// is set. `resize: both` looked like it worked and then snapped back, which
// is worse than a control that never offered the axis. Height is also the
// dimension that matters for a dashboard panel: how many rows you can see.

import { useEffect } from "react";

const KEY = "tradebot.panels.v1";

type Sizes = Record<string, number>;   // panel key -> height in px

function read(): Sizes {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "{}");
    // v1 stored {w,h} objects; keep only the height and drop the rest.
    const out: Sizes = {};
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      if (typeof v === "number") out[k] = v;
      else if (v && typeof v === "object" && typeof (v as { h?: number }).h === "number") {
        out[k] = (v as { h: number }).h;
      }
    }
    return out;
  } catch {
    return {};
  }
}

function write(sizes: Sizes): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(sizes));
  } catch {
    /* private mode — sizes just won't persist */
  }
}

/** Forget every stored height. Bound to the Settings button, because a panel
 *  dragged down to its 80px floor is otherwise awkward to grab again. */
export function resetPanelSizes(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}

// ponytail: panels are keyed by "<page>:<index>", not by a data-attribute on
// each widget — that keeps this to zero markup changes across 13 pages. The
// ceiling: reordering the panels on a page reassigns their saved heights.
// Give the widgets stable data-panel ids if that ever actually bites.
export function usePanelSizes(page: string): void {
  useEffect(() => {
    // Dataset opts out: its panels wrap one very wide table, so the grip
    // belongs on the table's scroll box (see App.css §36), not the panel.
    // Returning early also stops previously-stored heights being re-applied
    // to panels that can no longer be resized back.
    if (page === "dataset") return;

    let panels: HTMLElement[] = [];
    let saveTimer: number | undefined;
    // Restoring a height triggers the ResizeObserver, which would then save
    // the *observed* height straight back. On a grid that reads as the
    // clamped value, so every reload rewrote the stored size a little
    // smaller until the panel collapsed. Ignore observations we caused.
    let restoring = false;

    const observer = new ResizeObserver((entries) => {
      if (restoring) return;
      window.clearTimeout(saveTimer);
      saveTimer = window.setTimeout(() => {
        const next = read();
        let dirty = false;
        for (const entry of entries) {
          const el = entry.target as HTMLElement;
          const i = panels.indexOf(el);
          // Only record a height the operator set by hand. An untouched
          // panel has no inline height, and storing its measured height
          // would freeze a responsive layout at its current size.
          if (i < 0 || !el.style.height) continue;
          next[`${page}:${i}`] = Math.round(el.offsetHeight);
          dirty = true;
        }
        if (dirty) write(next);
      }, 300);
    });

    // Pages are lazy() + Suspense, so on the first effect the panels do not
    // exist yet — scanning once here found nothing and silently did nothing.
    // Re-scan whenever the page content changes instead.
    const attach = () => {
      const found = Array.from(document.querySelectorAll<HTMLElement>(".widget"));
      if (found.length === panels.length && found.every((el, i) => el === panels[i])) {
        return;
      }
      observer.disconnect();
      panels = found;
      const sizes = read();
      restoring = true;
      panels.forEach((el, i) => {
        const h = sizes[`${page}:${i}`];
        if (h) el.style.height = `${h}px`;
        observer.observe(el);
      });
      // Let the restore's own resize events flush before listening again.
      window.setTimeout(() => {
        restoring = false;
      }, 400);
    };

    attach();
    let attachTimer: number | undefined;
    const mutations = new MutationObserver(() => {
      window.clearTimeout(attachTimer);
      attachTimer = window.setTimeout(attach, 50);
    });
    mutations.observe(document.body, { childList: true, subtree: true });

    return () => {
      window.clearTimeout(saveTimer);
      window.clearTimeout(attachTimer);
      mutations.disconnect();
      observer.disconnect();
    };
  }, [page]);
}
