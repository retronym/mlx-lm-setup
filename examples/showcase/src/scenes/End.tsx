import React from "react";
import { useCurrentFrame } from "remotion";
import { Block, Heading, Mono, OUTQ, prog, sceneFrames, useAllScenes, useCue, useScene } from "../lib";
import { RIBBON, RibbonBar, pxPerGb } from "../Ribbon";
import { C, MONO } from "../theme";

/** Bookend: the ribbon from the cold open, calm, while this film renders. */
export const End: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const all = useAllScenes(), sc = useScene();
  const durationInFrames = sceneFrames(sc);
  const total = all.reduce((n, s) => n + sceneFrames(s), 0);
  const frameNo = total - durationInFrames + f;            // this frame's number in the whole film
  const k = pxPerGb();
  const blocks = [
    { label: "macOS + apps", gb: 8, color: C.os },
  ];
  let x = 0;
  const zero = prog(f, cue("zero"), 16);
  const outro = prog(f, durationInFrames - 75, 30);
  const render = Math.min(1, frameNo / total);
  return (
    <>
      <div style={{ position: "absolute", inset: 0, opacity: 1 - outro }}>
        <div style={{ position: "absolute", left: RIBBON.x, top: 200, width: RIBBON.w }}>
          <Mono size={20}>rendering showcase.mp4 · frame {frameNo} of {total}</Mono>
          <div style={{ marginTop: 12, height: 6, borderRadius: 3, background: C.line }}>
            <div style={{ width: `${render * 100}%`, height: "100%", borderRadius: 3, background: C.ink }} />
          </div>
        </div>
        <RibbonBar>
          {blocks.map((b, i) => {
            const p = prog(f, 4 + i * 6, 16, OUTQ);
            const el = <Block key={b.label} x={x * k + 2} y={3 - (1 - p) * 60} w={b.gb * k - 4} h={RIBBON.h - 6} color={b.color} label={b.label} gb={b.gb} opacity={p} />;
            x += b.gb;
            return el;
          })}
          <div style={{ position: "absolute", left: 8 * k + 30, top: 22, opacity: prog(f, 30, 20) }}>
            <Mono size={22} color={C.muted}>models: every one passivated after its narration · 0 GB</Mono>
          </div>
        </RibbonBar>
        <div style={{ position: "absolute", left: RIBBON.x + RIBBON.w - 360, top: RIBBON.y + RIBBON.h + 64, width: 360, height: 44, borderRadius: 8,
                      boxShadow: `inset 0 0 0 2px ${zero > 0 ? C.good : C.line}`, background: "#100e0b" }}>
          <div style={{ position: "absolute", right: 380, top: 8, fontFamily: MONO, fontSize: 20, color: zero > 0 ? C.good : C.faint, whiteSpace: "nowrap" }}>
            swap growth {zero > 0 ? "none ✓" : "…"}
          </div>
        </div>
      </div>
      <div style={{ position: "absolute", left: 0, right: 0, top: 420, textAlign: "center", opacity: outro }}>
        <Heading size={80}>mlx-lm-setup</Heading>
        <div style={{ marginTop: 18 }}><Mono size={30}>a private AI back end for one Mac · 127.0.0.1:8090</Mono></div>
      </div>
    </>
  );
};
