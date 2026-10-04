import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { SceneT, Word } from "./lib";
import { BODY, C, FPS } from "./theme";

type Line = { start: number; end: number; words: Word[] };

/** Group timed words into caption lines: break at sentence ends, long pauses, or about 8 words; hold through short gaps. */
const lines = (words: Word[]): Line[] => {
  const out: Word[][] = []; let cur: Word[] = [];
  for (const w of words) {
    if (cur.length && (cur.length >= 8 || w.s - cur[cur.length - 1].e > 0.6)) { out.push(cur); cur = []; }
    cur.push(w);
    if (/[.?!]$/.test(w.w) || (/,$/.test(w.w) && cur.length >= 5)) { out.push(cur); cur = []; }
  }
  if (cur.length) out.push(cur);
  const ls = out.map(l => ({ start: l[0].s, end: l[l.length - 1].e + 0.25, words: l }));
  ls.forEach((l, i) => { const n = ls[i + 1]; if (n && n.start - l.end < 0.8) l.end = n.start; });
  return ls;
};

export const Captions: React.FC<{ scenes: SceneT[]; starts: number[] }> = ({ scenes, starts }) => {
  const f = useCurrentFrame();
  const i = starts.findIndex((s, k) => f >= s && (k === starts.length - 1 || f < starts[k + 1]));
  if (i < 0) return null;
  const sc = scenes[i];
  const t = (f - starts[i]) / FPS - sc.lead_s;
  const line = lines(sc.words).find(l => t >= l.start - 0.05 && t < l.end);
  if (!line) return null;
  const op = Math.min(interpolate(t, [line.start - 0.05, line.start + 0.1], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
                      interpolate(t, [line.end - 0.12, line.end], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }));
  return (
    <div style={{ position: "absolute", left: 0, right: 0, bottom: 44, display: "flex", justifyContent: "center", opacity: op }}>
      <div style={{ maxWidth: 1400, padding: "10px 26px", borderRadius: 12, background: "rgba(10,9,7,0.72)", fontFamily: BODY, fontSize: 34,
                    fontWeight: 500, lineHeight: 1.3, textAlign: "center" }}>
        {line.words.map((w, k) => (
          <span key={k} style={{ color: t >= w.s ? C.ink : "rgba(241,236,227,0.55)" }}>{w.w}{k < line.words.length - 1 ? " " : ""}</span>
        ))}
      </div>
    </div>
  );
};
