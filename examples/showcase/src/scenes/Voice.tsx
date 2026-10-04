import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Arrow, Chip, Heading, INOUT, Mono, prog, typed, useAllScenes, useCue, useScene } from "../lib";
import { C, FPS } from "../theme";
import { PlanRail } from "../team";

// The narrator was designed on the speech page with its default description, then kept with save_as_voice.
const DESCRIPTION = "A calm, warm female narrator in her thirties, clear and measured, with a gentle smile in her voice.";


export const Wave: React.FC<{ peaks: number[]; w: number; h: number; upto?: number; color?: string }> = ({ peaks, w, h, upto = Infinity, color = C.speech }) => {
  const n = Math.min(peaks.length, Math.floor(w / 5));
  const step = peaks.length / n;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 2, width: w, height: h }}>
      {Array.from({ length: n }, (_, i) => {
        const v = Math.max(...peaks.slice(Math.floor(i * step), Math.floor((i + 1) * step)), 0.02);
        const on = i * step < upto;
        return <div key={i} style={{ width: 3, height: Math.max(2, v * h), borderRadius: 2, background: on ? color : C.line }} />;
      })}
    </div>
  );
};

/** The reveal: this narration is the demo. Its own waveform draws as it is spoken, and Whisper's word timings appear under it. */
export const Voice: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const sc = useScene();
  const all = useAllScenes();
  const t = f / FPS - sc.lead_s;
  const W = 1600, X = 160, Y = 170, H = 150;
  const wordsOn = prog(f, cue("words"), 16);
  const pipeOp = 1;

  return (
    <>
      <PlanRail current={3} opacity={1 - prog(f, 0, 30)} />
      {/* this very narration */}
      <div style={{ position: "absolute", left: X, top: Y - 50 }}><Mono size={20}>this narration · voice.wav</Mono></div>
      <div style={{ position: "absolute", left: X, top: Y }}>
        <Wave peaks={sc.peaks} w={W} h={H} upto={t * FPS} />
      </div>
      {wordsOn > 0 && sc.words.filter(w => w.s <= t).map((w, i) => {
        const x = X + (w.s / sc.duration_s) * W;
        return (
          <div key={i} style={{ position: "absolute", left: x, top: Y + H + 8 + (i % 3) * 26, opacity: wordsOn }}>
            <div style={{ width: 2, height: 12 + (i % 3) * 26, background: C.muted, position: "absolute", top: -(8 + (i % 3) * 26) }} />
            <Mono size={15} color={C.ink}>{w.w.replace(/[.,]$/, "")}</Mono>
          </div>
        );
      })}

      <div style={{ position: "absolute", inset: 0, opacity: pipeOp }}>
        {/* design once ... */}
        <Appear at={cue("design")} style={{ left: 160, top: 470, width: 560 }}>
          <Mono size={20}>a sentence of description</Mono>
          <div style={{ marginTop: 10, padding: "18px 22px", borderRadius: 14, background: C.panel, border: `2px solid ${C.line}`, minHeight: 130 }}>
            <span style={{ fontFamily: "Georgia, serif", fontSize: 26, color: C.ink, lineHeight: 1.4 }}>“{typed(DESCRIPTION, f, cue("design") + 6, 40)}”</span>
          </div>
        </Appear>
        <Arrow x1={740} y1={560} x2={800} y2={560} p={prog(f, cue("design") + 40, 12)} color={C.muted} />
        <Appear at={cue("design") + 46} style={{ left: 810, top: 528 }}><Chip color={C.speech} size={24}>Qwen3-TTS design</Chip></Appear>
        <Arrow x1={1080} y1={560} x2={1140} y2={560} p={prog(f, cue("design") + 60, 12)} color={C.muted} />
        <Appear at={cue("design") + 66} style={{ left: 1150, top: 500 }}>
          <Mono size={20} color={C.ink}>data/voices/narrator.wav</Mono>
          <div style={{ marginTop: 8 }}><Wave peaks={(all.find(s => s.id === "hook") ?? sc).peaks} w={300} h={44} /></div>
        </Appear>
        {/* ... then clone it for every scene */}
        <Appear at={cue("clone")} style={{ left: 1150, top: 640 }}><Chip color={C.speech} size={24}>Qwen3-TTS clone</Chip></Appear>
        {all.map(s => s.id).map((id, i) => {
          const s = all.find(x => x.id === id);
          const p = prog(f, cue("clone") + 14 + i * 4, 14);
          return (
            <div key={id} style={{ position: "absolute", left: 1500 + (i % 3) * 100, top: 440 + Math.floor(i / 3) * 56, opacity: p, transform: `translateX(${(1 - p) * -30}px)` }}>
              <Mono size={14}>{id}</Mono>
              {s && <Wave peaks={s.peaks} w={90} h={22} color={id === "voice" ? C.accent : C.speech} />}
            </div>
          );
        })}
        <Appear at={cue("words")} style={{ left: 160, top: 760 }}>
          <Chip color={C.speech} size={24}>Whisper</Chip>
          <Mono size={20} style={{ marginLeft: 18 }}>listens back · a timestamp for every word · the cues that time this film</Mono>
        </Appear>
      </div>

    </>
  );
};
