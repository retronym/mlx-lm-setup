import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Arrow, Body, Chip, Dot, Heading, INOUT, Mono, along, lerp, prog, useCue, useData, useScene } from "../lib";
import { Card, JobCard } from "../team";
import { C, FPS, HEAD, MONO } from "../theme";
import { Wave } from "./Voice";

/** Hook: this narration's own waveform, and what was made locally. */
export const Hook: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const sc = useScene();
  const t = f / FPS - sc.lead_s;
  return (
    <>
      <div style={{ position: "absolute", left: 160, top: 300 }}><Wave peaks={sc.peaks} w={1600} h={180} upto={t * FPS} /></div>
      <div style={{ position: "absolute", left: 160, top: 560, display: "flex", gap: 20 }}>
        {[["the voice", "Qwen3-TTS"], ["the first draft", "Gemma 4"], ["the fact-check", "OpenJev 4B"]].map(([a, b], i) => (
          <Appear key={a} at={cue("ledger") + i * 14} style={{ position: "relative" }}>
            <div style={{ padding: "16px 24px", borderRadius: 14, background: C.panel, border: `2px solid ${C.line}`, minWidth: 300 }}>
              <Mono size={18}>{a}</Mono>
              <Heading size={36}>{b}</Heading>
              <Mono size={16} color={C.good}>on this Mac</Mono>
            </div>
          </Appear>
        ))}
      </div>
    </>
  );
};

// the deck's resting layout during act 1: a 2 x 4 grid, then a compact column on the left
const grid = (i: number): [number, number, number] => [160 + (i % 4) * 400, 300 + Math.floor(i / 4) * 200, ((i * 37) % 7) - 3];
const column = (i: number): [number, number, number] => [130, 170 + i * 98, 0];

export const Deal: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const deck = useData().cards.deck as Card[];
  return (
    <>
      <Appear at={0} style={{ left: 160, top: 120 }}><Heading size={54}>A stream of small jobs</Heading></Appear>
      {deck.map((c, i) => {
        const p = prog(f, 12 + i * Math.max(6, (cue("deal") + 40 - 12) / 8), 16);
        const [x, y, r] = grid(i);
        return p > 0 ? <JobCard key={c.id} card={c} x={lerp(1900, x, p)} y={y} rot={r * p + (1 - p) * 12} w={370} opacity={Math.min(1, p * 2)} /> : null;
      })}
    </>
  );
};

/** Cost, latency, privacy: one scene component, three modes. The deck shrinks to a column; three tabs signpost the reasons. */
const REASONS = ["cost", "latency", "privacy"];
export const Why: React.FC<{ mode: 0 | 1 | 2 }> = ({ mode }) => {
  const f = useCurrentFrame();
  const cue = useCue();
  const { cards } = useData();
  const deck = cards.deck as Card[];
  const shrink = mode === 0 ? prog(f, 0, 24, INOUT) : 1;
  return (
    <>
      {deck.map((c, i) => {
        const [gx, gy, gr] = grid(i), [cx, cy] = column(i);
        const lock = mode === 2 && c.private ? prog(f, cue("lock") + i * 4, 10) : 0;
        return <JobCard key={c.id} card={c} x={lerp(gx, cx, shrink)} y={lerp(gy, cy, shrink)} rot={gr * (1 - shrink)} w={lerp(370, 330, shrink)}
                        small={shrink > 0.5} lock={lock} tag={lock > 0 ? "private" : undefined} />;
      })}
      {/* the three reasons, as tabs */}
      <div style={{ position: "absolute", left: 600, top: 120, display: "flex", gap: 48, opacity: shrink }}>
        {REASONS.map((r, i) => (
          <div key={r} style={{ fontFamily: HEAD, fontWeight: 700, fontSize: 46, color: i === mode ? C.ink : i < mode ? C.muted : C.faint,
                                borderBottom: `4px solid ${i === mode ? C.accent : "transparent"}`, paddingBottom: 6 }}>{i + 1} · {r}</div>
        ))}
      </div>
      <div style={{ position: "absolute", left: 600, top: 260, width: 1180, height: 640 }}>
        {mode === 0 && <Cost />}
        {mode === 1 && <Latency />}
        {mode === 2 && <Privacy />}
      </div>
    </>
  );
};

const fmt = (n: number) => Math.round(n).toLocaleString("en-GB");

const Cost: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const tr = useData().cards.triage;
  const per = tr.input_tokens_est / tr.judgments;
  const vol = prog(f, cue("volume"), 50);
  return (
    <>
      <Appear at={cue("one")} style={{ left: 0, top: 0 }}>
        <Mono size={22}>one judgment</Mono>
        <Heading size={64}>≈ {fmt(per)} tokens</Heading>
      </Appear>
      <Appear at={cue("volume")} style={{ left: 520, top: 0 }}>
        <Mono size={22}>{tr.prs} pull requests × {tr.questions} questions = {fmt(tr.judgments * vol)} judgments</Mono>
        <Heading size={64} color={C.accent}>≈ {fmt(tr.input_tokens_est * vol)} tokens</Heading>
      </Appear>
      <Appear at={cue("free")} style={{ left: 0, top: 230, width: 1100 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 28 }}>
          <div style={{ fontSize: 70, color: C.good, transform: `rotate(${(f - cue("free")) * 6}deg)` }}>↻</div>
          <div>
            <Heading size={48} color={C.good}>a re-take on this Mac: electricity</Heading>
            <Body size={24} color={C.muted}>so you try again, and again</Body>
          </div>
        </div>
      </Appear>
      <Mono size={16} color={C.faint} style={{ position: "absolute", left: 0, top: 520 }}>tokens estimated as characters ÷ 4 of the triage's real inputs (data/prs.json)</Mono>
    </>
  );
};

const Latency: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const loopT = ((f % 90) / 90);
  const localT = prog(f, cue("local"), 20, INOUT);
  return (
    <>
      <Mono size={22} style={{ position: "absolute", left: 0, top: 10 }}>hosted: out and back</Mono>
      <svg style={{ position: "absolute", left: 0, top: 0, overflow: "visible" }} width={1} height={1}>
        <path d="M 60 200 C 300 -40, 900 -40, 1100 120" stroke={C.line} strokeWidth={3} fill="none" strokeDasharray="10 10" />
        <path d="M 1100 160 C 900 330, 300 330, 60 220" stroke={C.line} strokeWidth={3} fill="none" strokeDasharray="10 10" />
      </svg>
      {["network", "queue", "rate limit"].map((l, i) => <Mono key={l} size={18} color={C.faint} style={{ position: "absolute", left: 320 + i * 230, top: 40 + (i === 1 ? -20 : 0) }}>{l}</Mono>)}
      {(() => { const t = loopT; const [x, y] = t < 0.5 ? cubic([60, 200], [300, -40], [900, -40], [1100, 120], t * 2) : cubic([1100, 160], [900, 330], [300, 330], [60, 220], t * 2 - 1); return <Dot x={x} y={y} r={9} color={C.muted} />; })()}
      <div style={{ position: "absolute", left: 1040, top: 110, width: 120, height: 60, borderRadius: 30, border: `2px solid ${C.line}`, display: "flex", alignItems: "center", justifyContent: "center" }}><Mono size={16}>cloud</Mono></div>
      <Appear at={cue("local") - 6} style={{ left: 0, top: 380 }}>
        <Mono size={22}>local: one hop</Mono>
      </Appear>
      {localT > 0 && (
        <>
          <Arrow x1={60} y1={470} x2={360} y2={470} p={localT} color={C.good} width={4} />
          <div style={{ position: "absolute", left: 400, top: 430, opacity: localT }}>
            <Heading size={54} color={C.good}>≈ 1 s per decision</Heading>
            <Mono size={20}>no network · no rate limit</Mono>
          </div>
        </>
      )}
    </>
  );
};
const cubic = (a: number[], b: number[], c: number[], d: number[], t: number) => {
  const u = 1 - t;
  return [0, 1].map(k => u * u * u * a[k] + 3 * u * u * t * b[k] + 3 * u * t * t * c[k] + t * t * t * d[k]);
};

const Privacy: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const out = prog(f, cue("loopback") - 30, 30, INOUT);
  const cut = prog(f, cue("loopback") + 4, 10);
  return (
    <>
      <div style={{ position: "absolute", left: 0, top: 40, width: 640, height: 420, borderRadius: 24, border: `2px dashed ${C.line}` }} />
      <Mono size={18} style={{ position: "absolute", left: 24, top: 56 }}>this Mac</Mono>
      <div style={{ position: "absolute", left: 220, top: 220, padding: "10px 18px", borderRadius: 10, border: `2px solid ${C.accent}` }}><Mono size={20} color={C.ink}>127.0.0.1</Mono></div>
      <Arrow x1={400} y1={240} x2={lerp(400, 1000, out)} y2={240} p={out > 0 ? 1 : 0} head={cut === 0} color={cut > 0 ? C.faint : C.muted} width={3} dash={cut > 0 ? "8 10" : undefined} />
      {cut > 0 && <div style={{ position: "absolute", left: 618, top: 196, fontSize: 64, color: C.accent, opacity: cut, fontFamily: HEAD }}>✕</div>}
      <div style={{ position: "absolute", left: 1000, top: 200, width: 140, height: 80, borderRadius: 40, border: `2px solid ${C.line}`, display: "flex", alignItems: "center", justifyContent: "center", opacity: 0.6 }}><Mono size={18}>elsewhere</Mono></div>
      <Appear at={cue("loopback") + 10} style={{ left: 0, top: 500 }}><Body size={30}>The gateway listens only on the machine itself.</Body></Appear>
    </>
  );
};

/** The catch: a card flips over to a confidently wrong answer. */
export const Catch: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const flip = prog(f, cue("catch"), 20, INOUT);
  const front = flip < 0.5;
  return (
    <>
      <div style={{ position: "absolute", left: 660, top: 330, width: 600, height: 260, transform: `perspective(1200px) rotateY(${flip * 180}deg)`, transformStyle: "preserve-3d" }}>
        <div style={{ position: "absolute", inset: 0, borderRadius: 16, background: front ? "#f1ece3" : C.panel, border: front ? "none" : `3px solid ${C.accent}`,
                      transform: front ? undefined : "rotateY(180deg)", padding: 30, boxSizing: "border-box", boxShadow: "0 20px 60px rgba(0,0,0,0.5)" }}>
          {front ? (
            <><Body size={30} color="#15130f">Review this function: moving_avg(xs, n)</Body><div style={{ marginTop: 16 }}><Mono size={18} color="#55503f">to a local model</Mono></div></>
          ) : (
            <><Body size={30}>“It raises an <b>IndexError</b> when n is larger than the list.”</Body>
              <div style={{ marginTop: 22, display: "flex", gap: 14 }}><Chip color={C.accent} size={24}>✗ invented</Chip><Chip color={C.accent} size={24}>said with confidence</Chip></div></>
          )}
        </div>
      </div>
      <Appear at={cue("catch") + 30} style={{ left: 660, top: 640 }}><Mono size={22}>smaller models · sometimes confidently wrong</Mono></Appear>
    </>
  );
};

export const Cost_ = () => <Why mode={0} />;
export const Latency_ = () => <Why mode={1} />;
export const Privacy_ = () => <Why mode={2} />;
export { along };
