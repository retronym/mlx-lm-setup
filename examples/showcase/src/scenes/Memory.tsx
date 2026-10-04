import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Block, Body, Heading, INOUT, Mono, lerp, prog, useCue, useData } from "../lib";
import { RIBBON, RibbonBar } from "../Ribbon";
import { C, FAMILY, LABEL } from "../theme";

const ORDER = [["qwen3-6-35b-a3b", "qwen3-coder", "kokoro", "whisper"], ["gemma-4-26b-a4b", "openjev-4b", "jevstyle-2b", "qwen3-tts-design", "qwen3-tts-clone"]];

/** The whole catalog, to scale, against the 28 GB budget: it does not fit, several times over. */
export const Memory: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const { sizes, budget_gb } = useData();
  const total = Object.values(sizes).reduce((a, b) => a + b, 0);

  const fall = prog(f, cue("overflow"), 40, INOUT);                        // shelf -> one long row on the ribbon
  const back = prog(f, cue("budget") + 30, 30, INOUT);                     // ... and back to the shelf, dimmed
  const zoomGb = lerp(RIBBON.gb, total + 4, fall * (1 - back));           // the ribbon zooms out to show the overflow
  const k = RIBBON.w / zoomGb;
  const k0 = RIBBON.w / RIBBON.gb;
  const barW = RIBBON.gb * k;

  let rowStart = 0;
  const blocks = ORDER.flatMap((row, r) => {
    let x = 0;
    return row.map((id, i) => {
      const b = { id, gb: sizes[id], sx: RIBBON.x + x, sy: 250 + r * 110, ex: RIBBON.x + rowStart * k, g0: rowStart, i: r * 5 + i };
      x += sizes[id] * k0 + 16; rowStart += sizes[id];
      return b;
    });
  });

  const speed = prog(f, cue("speed"), 16);
  const dimSpeed = prog(f, cue("memory"), 20, INOUT);
  const question = prog(f, cue("budget") + 50, 20);
  const bracket = prog(f, cue("overflow") + 40, 20) * (1 - back);
  return (
    <>
      <Appear at={0} until={cue("overflow") - 10} style={{ left: RIBBON.x, top: 120 }}>
        <Heading size={56}>The constraint is memory</Heading>
      </Appear>
      <div style={{ position: "absolute", right: 160, top: 110, opacity: speed * (1 - 0.7 * dimSpeed), textAlign: "right" }}>
        <Mono size={20}>generation</Mono>
        <div style={{ fontFamily: "inherit" }}><Heading size={60} color={C.good}>≈100 tok/s</Heading></div>
        <Mono size={20} color={C.good}>fast enough ✓</Mono>
      </div>

      <RibbonBar w={barW} ticks={zoomGb > 60 ? 12 : 4} budget={budget_gb} budgetP={prog(f, 10, 20)} label="48 GB unified memory">
        <div style={{ position: "absolute", left: budget_gb * k + 14, top: RIBBON.h + 40, fontFamily: "inherit", opacity: prog(f, 20, 20) * (1 - fall) }}>
          <Mono size={20}>macOS, the JVM, sbt, you</Mono>
        </div>
      </RibbonBar>
      {/* beyond physical memory */}
      {fall > 0 && (
        <div style={{ position: "absolute", left: RIBBON.x + barW + 6, top: RIBBON.y, width: (zoomGb - RIBBON.gb) * k - 6, height: RIBBON.h, borderRadius: 10,
                      border: `2px dashed ${C.accent}`, opacity: fall * (1 - back) * 0.9 }}>
          <Mono size={20} color={C.accent} style={{ position: "absolute", right: 16, top: RIBBON.h + 14 }}>does not exist</Mono>
        </div>
      )}

      {blocks.map(b => {
        const appear = prog(f, 8 + b.i * 4, 16);
        const t = fall * (1 - back);
        const x = lerp(b.sx, b.ex, t), y = lerp(b.sy, RIBBON.y + 3, t);
        const w = b.gb * lerp(k0, k, t) - 4;
        const over = b.g0 + b.gb > budget_gb;
        return <Block key={b.id} x={x} y={y + (1 - appear) * 20} w={w} h={RIBBON.h - 6} color={FAMILY[b.id]} label={LABEL[b.id]} gb={b.gb}
                      opacity={appear * (1 - 0.55 * back)} outline={t > 0.9 && over ? C.accent : undefined} dim={back * 0.6} />;
      })}

      {bracket > 0 && (
        <div style={{ position: "absolute", left: RIBBON.x, top: RIBBON.y - 120, width: total * k, opacity: bracket }}>
          <div style={{ height: 14, borderTop: `3px solid ${C.ink}`, borderLeft: `3px solid ${C.ink}`, borderRight: `3px solid ${C.ink}`, marginTop: 60 }} />
          <div style={{ position: "absolute", top: 0, left: 0, right: 0, textAlign: "center" }}>
            <Heading size={44}>{(total * Math.min(1, bracket * 1.2)).toFixed(0)} GB wanted <span style={{ color: C.accent }}>· {(total / budget_gb).toFixed(0)}× the budget</span></Heading>
          </div>
        </div>
      )}
      {question > 0 && (
        <div style={{ position: "absolute", left: RIBBON.x, top: 520, opacity: question, transform: `translateY(${(1 - question) * 12}px)` }}>
          <Body size={40} color={C.ink}>Who is resident, and when?</Body>
        </div>
      )}
    </>
  );
};
