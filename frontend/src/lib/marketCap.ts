// Market-cap tier for a ticker, per the SEBI categorisation AMFI
// publishes twice a year:
//   rank 1–100   = Large cap
//   rank 101–250 = Mid cap
//   rank 251+    = Small cap  (~5,200 names — everything else)
//
// Nothing in the Fyers scrip master or in a quote carries market cap, so
// the tiers come from AMFI's list. `marketCapData.ts` is generated from
// the workbook in data/amfi/ by scripts/gen_market_cap.py — only the top
// 250 are shipped, since "absent from both sets" reproduces the Small
// rows exactly. Refresh after each half-yearly AMFI release:
//   python scripts/gen_market_cap.py           (regenerate)
//   python scripts/gen_market_cap.py --check   (verify against the xlsx)

import { LARGE, MID } from "./marketCapData";

export type CapTier = "Large" | "Mid" | "Small";

// Badge colour per tier — reuses existing .badge variants in App.css.
export const CAP_BADGE_CLASS: Record<CapTier, string> = {
  Large: "accent",
  Mid: "info",
  Small: "neutral",
};

/** Cap tier for a ticker in any form the bot uses: "SBIN",
 *  "NSE:SBIN-EQ", "BSE:SBIN-A". Unknown names are Small. */
export function capTier(symbol: string | null | undefined): CapTier {
  const bare = (symbol ?? "")
    .trim()
    .toUpperCase()
    .split(":")
    .pop()!
    .replace(/-(EQ|BE|BZ|SM|ST|IL|A|B|T|X)$/, "");
  if (LARGE.has(bare)) return "Large";
  if (MID.has(bare)) return "Mid";
  return "Small";
}
