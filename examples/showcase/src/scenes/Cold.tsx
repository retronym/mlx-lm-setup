import React from "react";
import { random, useCurrentFrame } from "remotion";
import { Block, Mono, OUTQ, prog, useAt, useCue } from "../lib";
import { RIBBON, RibbonBar, pxPerGb } from "../Ribbon";
import { C, MONO } from "../theme";

/** The cold open, and the state the title scene starts from. */
export const coldBlocks = (at: (t: number) => number, cue: (n: string) => number) => [
  { id: "os", label: "macOS + apps", gb: 8, color: C.os, f: at(-0.6) },
  { id: "q1", label: "Qwen3-Coder", gb: 17.5, color: C.llm, f: at(0.9) },
  { id: "q2", label: "Qwen3-Coder, again", gb: 17.5, color: C.accent, f: cue("dup") + 8 },
  { id: "nli", label: "OpenJev 4B", gb: 9.5, color: C.nli, f: cue("nli") + 4 },
  { id: "br", label: "browsers", gb: 1.5, color: "#7d7466", f: cue("browsers") + 6 },
];
const SWAP_CAP = 7, DROP = 16;

export const ColdRibbon: React.FC<{ frame: number; blocks: ReturnType<typeof coldBlocks>; lift?: (i: number) => number }> = ({ frame, blocks, lift = () => 0 }) => {
  const k = pxPerGb();
  let used = 0;
  const parts = blocks.map((b, i) => {
    const p = prog(frame, b.f, DROP, OUTQ);
    const start = used; used += b.gb * p;
    return { ...b, p, start, l: lift(i) };
  });
  const spill = Math.max(0, used - RIBBON.gb);
  const spillShown = parts.reduce((s, b) => s + Math.max(0, Math.min(b.start + b.gb * b.p, b.start + b.gb) - Math.max(b.start, RIBBON.gb)) * (1 - b.l), 0);
  const trayW = SWAP_CAP * k * 1.6;
  return (
    <>
      <RibbonBar>
        {parts.map(b => {
          const vis = Math.min(b.gb, Math.max(0, RIBBON.gb - b.start));
          return b.p > 0 && vis > 0 ? (
            <Block key={b.id} x={b.start * k + 2} y={-(1 - b.p) * 140 - b.l * 60 + 3} w={vis * k - 4} h={RIBBON.h - 6} color={b.color}
                   label={b.label} gb={b.gb} opacity={Math.min(1, b.p * 3) * (1 - b.l)} />
          ) : null;
        })}
      </RibbonBar>
      {/* swap tray under the right end of the bar */}
      <div style={{ position: "absolute", left: RIBBON.x + RIBBON.w - trayW, top: RIBBON.y + RIBBON.h + 64, width: trayW, height: 44, borderRadius: 8,
                    boxShadow: `inset 0 0 0 2px ${C.line}`, background: "#100e0b", opacity: spill > 0 || spillShown > 0 ? 1 : 0.35 }}>
        <div style={{ position: "absolute", left: 3, top: 3, bottom: 3, width: Math.min(1, spillShown / SWAP_CAP) * (trayW - 6), borderRadius: 6,
                      background: `repeating-linear-gradient(135deg, ${C.accent} 0 8px, #b2461c 8px 16px)` }} />
        <div style={{ position: "absolute", right: trayW + 18, top: 8, fontFamily: MONO, fontSize: 20, color: spillShown > 0.1 ? C.accent : C.faint, whiteSpace: "nowrap" }}>
          swap {spillShown.toFixed(1)} of {SWAP_CAP} GB
        </div>
      </div>
      {spillShown > 0.05 && (
        <svg style={{ position: "absolute", left: 0, top: 0 }} width={1920} height={1080}>
          <path d={`M ${RIBBON.x + RIBBON.w + 8} ${RIBBON.y + RIBBON.h / 2} C ${RIBBON.x + RIBBON.w + 60} ${RIBBON.y + RIBBON.h / 2}, ${RIBBON.x + RIBBON.w + 40} ${RIBBON.y + RIBBON.h + 86}, ${RIBBON.x + RIBBON.w + 4} ${RIBBON.y + RIBBON.h + 86}`}
                stroke={C.accent} strokeWidth={3} fill="none" strokeDasharray="8 8" strokeDashoffset={-frame} opacity={Math.min(1, spillShown)} />
        </svg>
      )}
    </>
  );
};

/** Memory pressure 0..1 as a function of what has dropped in so far. */
const pressureAt = (f: number, blocks: ReturnType<typeof coldBlocks>) => {
  const used = blocks.reduce((s, b) => s + b.gb * prog(f, b.f, DROP, OUTQ), 0);
  return Math.min(1, Math.max(0, (used - 6) / (RIBBON.gb + 4 - 6)));
};
const pressureColor = (p: number) => (p < 0.55 ? C.good : p < 0.85 ? C.warn : C.accent);

export const Cold: React.FC = () => {
  const real = useCurrentFrame();
  const at = useAt(), cue = useCue();
  const blocks = coldBlocks(at, cue);
  const stutterFrom = cue("swap") + 20;
  // the machine stutters: time advances in jerks and the frame jolts
  const frame = real < stutterFrom ? real : stutterFrom + Math.floor((real - stutterFrom) / 7) * 7;
  const jolt = real >= stutterFrom ? (random(`j${Math.floor(real / 7)}`) - 0.5) * 8 : 0;

  const chart = { x: 160, y: 170, w: 1600, h: 300 };
  const N = Math.max(2, frame);
  const pts: [number, number, number][] = [];
  const span = at(13.2);                    // the chart's time axis spans the scene
  for (let f = 0; f <= N; f += 3) {
    const p = pressureAt(f, blocks);
    pts.push([chart.x + (f / span) * chart.w, chart.y + chart.h - (0.08 + p * 0.86) * chart.h, p]);
  }
  const ball = prog(real, cue("swap") + 10, 12);
  return (
    <div style={{ position: "absolute", inset: 0, transform: `translate(${jolt}px, ${jolt * 0.4}px)` }}>
      <div style={{ position: "absolute", left: chart.x, top: chart.y - 50, fontFamily: MONO, fontSize: 20, color: C.muted }}>memory pressure</div>
      <div style={{ position: "absolute", left: chart.x, top: chart.y, width: chart.w, height: chart.h, borderLeft: `2px solid ${C.line}`, borderBottom: `2px solid ${C.line}` }} />
      <svg style={{ position: "absolute", left: 0, top: 0 }} width={1920} height={1080}>
        {pts.slice(1).map((p, i) => (
          <line key={i} x1={pts[i][0]} y1={pts[i][1]} x2={p[0]} y2={p[1]} stroke={pressureColor(p[2])} strokeWidth={5} strokeLinecap="round" />
        ))}
      </svg>
      <ColdRibbon frame={frame} blocks={blocks} />
      {ball > 0 && (
        <div style={{ position: "absolute", left: 1650, top: 230, width: 80, height: 80, borderRadius: 40, opacity: ball,
                      background: "conic-gradient(#e94b3c, #f2a93b, #f5e04b, #5fc35a, #3b9de9, #9c5ae9, #e94b3c)",
                      transform: `rotate(${real * 24}deg) scale(${0.6 + 0.4 * ball})`, boxShadow: "0 0 30px rgba(0,0,0,0.6)" }} />
      )}
      <Mono size={18} color={C.faint} style={{ position: "absolute", left: chart.x, top: chart.y + chart.h + 14 }}>October 3, evening</Mono>
    </div>
  );
};
