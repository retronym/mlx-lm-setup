import React, { createContext, useContext } from "react";
import { Easing, Img, interpolate, staticFile, useCurrentFrame } from "remotion";
import { BODY, C, FPS, HEAD, MONO } from "./theme";

export type Word = { w: string; s: number; e: number };
export type SceneT = {
  id: string; audio: string; duration_s: number; lead_s: number; tail_s: number; text: string;
  cues: Record<string, number>; words: Word[]; peaks: number[];
};
export type Roc = { label: string; auc: number; positives: number; n: number; curve: [number, number][]; at_half: [number, number]; precision_at_half: number; flagged_at_half: number };
export type Emoji = { e: string; name: string; p: number; z: number };
export type Data = {
  sizes: Record<string, number>; budget_gb: number;
  phrase: { text: string; before: string; after: string; emoji: Emoji[]; top: Emoji[];
            attrs: { color: { top: string; p: number }; mood: { top: string; p: number; probs: Record<string, number> }; sentiment: { score: number } } };
  book_sample: { text: string; top: string[]; color: string }[]; book_phrases: number;
  prs: { number: number; title: string }[]; rocs: Roc[];
};
export type Props = { timeline: { fps: number; scenes: SceneT[] }; data: Data };

export const SceneCtx = createContext<SceneT | null>(null);
export const DataCtx = createContext<Data | null>(null);
export const useScene = () => useContext(SceneCtx)!;
export const useData = () => useContext(DataCtx)!;

/** Frame (relative to the scene) at which a narration cue is spoken. */
export const useCue = () => {
  const s = useScene();
  return (name: string, offset_s = 0) => {
    const t = s.cues[name];
    if (t === undefined) throw new Error(`scene ${s.id}: no cue ${name}`);
    return Math.round((s.lead_s + t + offset_s) * FPS);
  };
};
/** Frame (relative to the scene) of a time in the narration. */
export const useAt = () => {
  const s = useScene();
  return (t_s: number) => Math.round((s.lead_s + t_s) * FPS);
};
export const sceneFrames = (s: SceneT) => Math.round((s.lead_s + s.duration_s + s.tail_s) * FPS);

export const OUT = Easing.out(Easing.cubic);
export const INOUT = Easing.inOut(Easing.cubic);
export const OUTQ = Easing.out(Easing.poly(5));

/** 0..1 progress of an animation that starts at `start` and lasts `dur` frames. */
export const prog = (frame: number, start: number, dur: number, easing = OUT) =>
  interpolate(frame, [start, start + Math.max(dur, 1)], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing });
export const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

/** Fade/slide in at `start`, optionally out at `end`. */
export const Appear: React.FC<{ at: number; until?: number; dy?: number; dx?: number; dur?: number; style?: React.CSSProperties; children: React.ReactNode }> =
  ({ at, until, dy = 18, dx = 0, dur = 14, style, children }) => {
    const f = useCurrentFrame();
    const i = prog(f, at, dur);
    const o = until === undefined ? 0 : prog(f, until, dur, INOUT);
    const op = i * (1 - o);
    if (op <= 0.001) return null;
    return <div style={{ position: "absolute", opacity: op, transform: `translate(${(1 - i) * dx}px, ${(1 - i) * dy - o * 10}px)`, ...style }}>{children}</div>;
  };

export const Mono: React.FC<{ size?: number; color?: string; style?: React.CSSProperties; children: React.ReactNode }> =
  ({ size = 22, color = C.muted, style, children }) => <span style={{ fontFamily: MONO, fontSize: size, color, ...style }}>{children}</span>;

export const Heading: React.FC<{ size?: number; color?: string; style?: React.CSSProperties; children: React.ReactNode }> =
  ({ size = 64, color = C.ink, style, children }) => (
    <div style={{ fontFamily: HEAD, fontWeight: 700, fontSize: size, color, letterSpacing: -0.5, lineHeight: 1.08, ...style }}>{children}</div>
  );

export const Body: React.FC<{ size?: number; color?: string; style?: React.CSSProperties; children: React.ReactNode }> =
  ({ size = 28, color = C.ink, style, children }) => <div style={{ fontFamily: BODY, fontSize: size, color, lineHeight: 1.35, ...style }}>{children}</div>;

/** A rounded chip with a label, e.g. a model or a state. */
export const Chip: React.FC<{ color?: string; fill?: string; size?: number; mono?: boolean; style?: React.CSSProperties; children: React.ReactNode }> =
  ({ color = C.line, fill = C.panel, size = 24, mono, style, children }) => (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 10, padding: "8px 18px", borderRadius: 12, border: `2px solid ${color}`,
                  background: fill, color: C.ink, fontFamily: mono ? MONO : BODY, fontSize: size, fontWeight: 500, whiteSpace: "nowrap", ...style }}>{children}</div>
  );

/** A model block drawn to scale: width is memory. */
export const Block: React.FC<{ x: number; y: number; w: number; h: number; color: string; label?: string; gb?: number; opacity?: number;
                               glow?: number; outline?: string; dim?: number; style?: React.CSSProperties; children?: React.ReactNode }> =
  ({ x, y, w, h, color, label, gb, opacity = 1, glow = 0, outline, dim = 0, style, children }) => (
    <div style={{ position: "absolute", left: x, top: y, width: Math.max(w, 0), height: h, opacity, borderRadius: 8, overflow: "hidden",
                  background: color, filter: dim ? `saturate(${1 - dim * 0.8}) brightness(${1 - dim * 0.55})` : undefined,
                  boxShadow: `${glow ? `0 0 ${40 * glow}px ${color}` : ""}${glow && outline ? "," : ""}${outline ? `inset 0 0 0 3px ${outline}` : ""}` || undefined, ...style }}>
      {label && w > 70 && (
        <div style={{ position: "absolute", left: 12, top: 0, bottom: 0, right: 8, display: "flex", flexDirection: "column", justifyContent: "center",
                      color: "#0f0d0a", fontFamily: BODY, fontWeight: 600, fontSize: h > 60 ? 21 : 17, lineHeight: 1.1, whiteSpace: "nowrap", overflow: "hidden" }}>
          <span>{label}</span>
          {gb !== undefined && w > 110 && <span style={{ fontFamily: MONO, fontWeight: 400, fontSize: h > 60 ? 18 : 15, opacity: 0.8 }}>{gb} GB</span>}
        </div>
      )}
      {children}
    </div>
  );

/** A real screenshot in a window frame, slowly pushed in. `focus` is the point (0..1 of the image) the push-in heads for. */
export const Capture: React.FC<{ src: string; x: number; y: number; w: number; label?: string; push?: number; focus?: [number, number];
                                 frame0?: number; dur?: number; opacity?: number; style?: React.CSSProperties; children?: React.ReactNode }> =
  ({ src, x, y, w, label, push = 0.12, focus = [0.5, 0.5], frame0 = 0, dur = 150, opacity = 1, style, children }) => {
    const f = useCurrentFrame();
    const p = prog(f, frame0, dur, INOUT);
    const s = 1 + push * p;
    const h = w * 900 / 1440;
    return (
      <div style={{ position: "absolute", left: x, top: y, width: w, opacity, borderRadius: 14, overflow: "hidden", background: "#0c0b09",
                    boxShadow: "0 30px 80px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.08)", ...style }}>
        <div style={{ height: 34, display: "flex", alignItems: "center", gap: 8, padding: "0 14px", background: "#2a2621" }}>
          {["#e0605a", "#e0b04a", "#6cbf5a"].map(c => <div key={c} style={{ width: 12, height: 12, borderRadius: 6, background: c, opacity: 0.85 }} />)}
          {label && <Mono size={15} style={{ marginLeft: 14 }}>{label}</Mono>}
        </div>
        <div style={{ position: "relative", width: w, height: h, overflow: "hidden" }}>
          <Img src={staticFile(src)} style={{ width: w, height: h, transform: `scale(${s})`, transformOrigin: `${focus[0] * 100}% ${focus[1] * 100}%` }} />
          {children}
        </div>
      </div>
    );
  };

/** Typewriter: reveal `text` from frame `at` at `cps` characters per second. */
export const typed = (text: string, frame: number, at: number, cps = 40) =>
  text.slice(0, Math.max(0, Math.floor(((frame - at) / FPS) * cps)));

export const Arrow: React.FC<{ x1: number; y1: number; x2: number; y2: number; color?: string; width?: number; p?: number; dash?: string; head?: boolean; bend?: number }> =
  ({ x1, y1, x2, y2, color = C.faint, width = 3, p = 1, dash, head = true, bend = 0 }) => {
    if (p <= 0) return null;
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 + bend;
    const d = `M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`;
    const len = Math.hypot(x2 - x1, y2 - y1) * (1 + Math.abs(bend) / 400);
    const ang = Math.atan2(y2 - my, x2 - mx);
    return (
      <svg style={{ position: "absolute", left: 0, top: 0, overflow: "visible", pointerEvents: "none" }} width={1} height={1}>
        <path d={d} stroke={color} strokeWidth={width} fill="none" strokeLinecap="round"
              strokeDasharray={dash ?? `${len} ${len}`} strokeDashoffset={dash ? 0 : len * (1 - p)} opacity={dash ? p : 1} />
        {head && p > 0.98 && (
          <path d={`M ${x2} ${y2} L ${x2 - 16 * Math.cos(ang - 0.45)} ${y2 - 16 * Math.sin(ang - 0.45)} L ${x2 - 16 * Math.cos(ang + 0.45)} ${y2 - 16 * Math.sin(ang + 0.45)} Z`} fill={color} />
        )}
      </svg>
    );
  };

/** Point along the same quadratic curve Arrow draws, for things travelling along it. */
export const along = (x1: number, y1: number, x2: number, y2: number, t: number, bend = 0) => {
  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 + bend;
  const u = 1 - t;
  return [u * u * x1 + 2 * u * t * mx + t * t * x2, u * u * y1 + 2 * u * t * my + t * t * y2];
};

export const Dot: React.FC<{ x: number; y: number; r?: number; color?: string; opacity?: number }> = ({ x, y, r = 10, color = C.accent, opacity = 1 }) =>
  <div style={{ position: "absolute", left: x - r, top: y - r, width: 2 * r, height: 2 * r, borderRadius: r, background: color, opacity, boxShadow: `0 0 18px ${color}` }} />;

export const Spinner: React.FC<{ x: number; y: number; r?: number; color?: string }> = ({ x, y, r = 14, color = C.ink }) => {
  const f = useCurrentFrame();
  return <div style={{ position: "absolute", left: x - r, top: y - r, width: 2 * r, height: 2 * r, borderRadius: r, border: `3px solid ${color}33`,
                       borderTopColor: color, transform: `rotate(${f * 14}deg)` }} />;
};
