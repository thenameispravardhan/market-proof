// Theme definitions for the AI News Trading Bot UI.
// Each theme is a complete set of CSS variable values that override the
// default design tokens declared in :root (App.css).
//
// IMPORTANT: themes only change colors / surfaces — never layout, never
// typography, never component structure. The dense, sharp, monospace
// Bloomberg-style aesthetic is preserved across every theme.

export type ThemeId =
  | "default"
  | "cyberpunk"
  | "glass"
  | "command"
  | "space"
  | "minimal"
  | "bio"
  | "synthwave"
  | "neural"
  | "arctic"
  | "sunset";

export interface ThemeMeta {
  id: ThemeId;
  name: string;
  /** Short subtitle shown under the theme name in the picker. */
  blurb: string;
  /** 4 swatch colors used in the picker card preview. */
  swatch: [string, string, string, string];
}

export const THEMES: ThemeMeta[] = [
  {
    id: "default",
    name: "Default",
    blurb: "Bloomberg terminal · amber on near-black",
    swatch: ["#0A0A0A", "#FF8C00", "#00D787", "#FF3838"],
  },
  {
    id: "cyberpunk",
    name: "Cyberpunk Neon",
    blurb: "Hot pink + electric blue, rainy city",
    swatch: ["#0A0414", "#FF2D8A", "#00D9FF", "#150826"],
  },
  {
    id: "glass",
    name: "Glassmorphism",
    blurb: "Soft purple + cyan, frosted panels",
    swatch: ["#1A0F2E", "#C084FC", "#67E8F9", "#251D44"],
  },
  {
    id: "command",
    name: "Sci-Fi Command",
    blurb: "Mission control · cyan + amber",
    swatch: ["#050B14", "#00D4FF", "#FFA726", "#0F1D36"],
  },
  {
    id: "space",
    name: "Deep Space",
    blurb: "Cosmic nebula · purple + blue",
    swatch: ["#050018", "#B484FF", "#4DCCFF", "#150C4A"],
  },
  {
    id: "minimal",
    name: "Minimal Mint",
    blurb: "Clean white + electric mint accent",
    swatch: ["#FAFAFA", "#00D68F", "#0A0A0A", "#F5F5F5"],
  },
  {
    id: "bio",
    name: "Bio-Organic",
    blurb: "Bioluminescent teal + emerald",
    swatch: ["#021410", "#2DD4BF", "#10B981", "#0A3A30"],
  },
  {
    id: "synthwave",
    name: "Retro Synthwave",
    blurb: "80s neon grid · magenta + cyan",
    swatch: ["#1A0033", "#FF00C8", "#00F0FF", "#3F1166"],
  },
  {
    id: "neural",
    name: "AI Neural",
    blurb: "Cyan + magenta, dark with glow",
    swatch: ["#04050D", "#00F0FF", "#FF00D4", "#111634"],
  },
  {
    id: "arctic",
    name: "Arctic Frost",
    blurb: "Ice blue + white, frozen light",
    swatch: ["#E6F4FF", "#0EA5E9", "#38BDF8", "#F0F8FF"],
  },
  {
    id: "sunset",
    name: "Sunset Cyber",
    blurb: "Blade Runner · orange + teal",
    swatch: ["#1A0A05", "#FF8C32", "#2DD4BF", "#3D1F0E"],
  },
];

export const DEFAULT_THEME: ThemeId = "default";

export function isThemeId(v: string | null | undefined): v is ThemeId {
  return THEMES.some((t) => t.id === v);
}
