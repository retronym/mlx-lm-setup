import { loadFont as loadGrotesk } from "@remotion/google-fonts/SpaceGrotesk";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";

export const HEAD = loadGrotesk("normal", { weights: ["500", "700"], subsets: ["latin"] }).fontFamily;
export const BODY = loadInter("normal", { weights: ["400", "500", "600"], subsets: ["latin"] }).fontFamily;
export const MONO = loadMono("normal", { weights: ["400", "600"], subsets: ["latin"] }).fontFamily;

export const W = 1920, H = 1080, FPS = 30;

// One accent (the warning orange) for "this is the danger"; green for "the compiler checked it"; blue for code and stations.
export const C = {
  bg: "#15130f",
  bg2: "#1d1a16",
  panel: "#242019",
  line: "#3b352d",
  ink: "#f1ece3",
  muted: "#a39a8c",
  faint: "#6b6357",
  accent: "#d95926",
  accentSoft: "rgba(217, 89, 38, 0.16)",
  good: "#6fb58a",
  warn: "#d9a33f",
  blue: "#5d8fd4",
  goodSoft: "rgba(111, 181, 138, 0.16)",
  kw: "#c792ea",
  str: "#c3d68a",
};
