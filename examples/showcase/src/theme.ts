import { loadFont as loadGrotesk } from "@remotion/google-fonts/SpaceGrotesk";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";

export const HEAD = loadGrotesk("normal", { weights: ["500", "700"], subsets: ["latin"] }).fontFamily;
export const BODY = loadInter("normal", { weights: ["400", "500", "600"], subsets: ["latin"] }).fontFamily;
export const MONO = loadMono("normal", { weights: ["400", "600"], subsets: ["latin"] }).fontFamily;

export const W = 1920, H = 1080, FPS = 30;

// One accent (the README's warning orange) for "this is the problem" and "the active thing"; muted hues per model family.
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
  llm: "#5d8fd4",
  decision: "#55ad8c",
  nli: "#9c80d0",
  speech: "#c9a14e",
  os: "#4a443b",
};

export const FAMILY: Record<string, string> = {
  "qwen3-coder": C.llm, "qwen3-6-35b-a3b": C.llm, "gemma-4-26b-a4b": C.llm,
  "openjev-4b": C.nli, "jevstyle-2b": C.decision,
  kokoro: C.speech, whisper: C.speech, "qwen3-tts-design": C.speech, "qwen3-tts-clone": C.speech,
};

export const LABEL: Record<string, string> = {
  "qwen3-coder": "Qwen3-Coder 30B", "qwen3-6-35b-a3b": "Qwen3.6 35B", "gemma-4-26b-a4b": "Gemma 4 26B",
  "openjev-4b": "OpenJev 4B", "jevstyle-2b": "Jev-Style 2B", kokoro: "Kokoro", whisper: "Whisper",
  "qwen3-tts-design": "TTS design", "qwen3-tts-clone": "TTS clone",
};
