import React from "react";
import { AbsoluteFill } from "remotion";
import { Arrow, Mono } from "../lib";
import { BODY, C, HEAD, MONO } from "../theme";

// A design sketch of the "team" device (storyboard v2): the lead above the office, interns at desks inside it.
const INTERNS = [
  { name: "Gemma 4 26B", gb: 16, job: "drafts, summaries", speed: "~85 tok/s", color: C.llm, on: true },
  { name: "Qwen3-Coder 30B", gb: 17.5, job: "boilerplate, extraction", speed: "~108 tok/s", color: C.llm, on: false },
  { name: "Jev-Style 2B", gb: 3, job: "sorting, labelling", speed: "~1 s / decision", color: C.decision, on: true },
  { name: "OpenJev 4B", gb: 9.5, job: "fact-checking", speed: "", color: C.nli, on: false },
  { name: "Qwen3-TTS", gb: 4.5, job: "voice", speed: "", color: C.speech, on: false },
  { name: "Whisper", gb: 3, job: "transcription, timing", speed: "", color: C.speech, on: false },
];

const Badge: React.FC<{ x: number; y: number; w: number; name: string; role: string; meta: string; color: string; on: boolean; lead?: boolean }> =
  ({ x, y, w, name, role, meta, color, on, lead }) => (
    <div style={{ position: "absolute", left: x, top: y, width: w, borderRadius: 14, background: C.panel, border: `2px solid ${on ? color : C.line}`,
                  boxShadow: on ? `0 0 30px ${color}33` : undefined, overflow: "hidden" }}>
      <div style={{ height: 8, background: color, opacity: on ? 1 : 0.35 }} />
      <div style={{ padding: "14px 18px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <Mono size={15} color={C.faint}>{lead ? "LEAD" : "INTERN"}</Mono>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <div style={{ width: 10, height: 10, borderRadius: 5, background: on ? C.good : C.line }} />
            <Mono size={14} color={on ? C.good : C.faint}>{on ? "in" : "out"}</Mono>
          </div>
        </div>
        <div style={{ fontFamily: HEAD, fontWeight: 700, fontSize: lead ? 34 : 24, color: C.ink, marginTop: 6 }}>{name}</div>
        <div style={{ fontFamily: BODY, fontSize: lead ? 22 : 18, color: C.ink, marginTop: 4 }}>{role}</div>
        <div style={{ fontFamily: MONO, fontSize: 14, color: C.muted, marginTop: 6 }}>{meta}</div>
      </div>
    </div>
  );

export const TeamSketch: React.FC = () => {
  const deskW = 250, gap = 22, x0 = 160 + (1600 - 6 * deskW - 5 * gap) / 2, deskY = 640;
  return (
    <AbsoluteFill style={{ background: `radial-gradient(ellipse 80% 70% at 50% 40%, ${C.bg2} 0%, ${C.bg} 70%)` }}>
      {/* plan rail */}
      <div style={{ position: "absolute", left: 160, right: 160, top: 34, display: "flex", gap: 40, fontFamily: MONO, fontSize: 18 }}>
        <span style={{ color: C.good }}>✓ 1 · how the interns are reached</span>
        <span style={{ color: C.ink, borderBottom: `2px solid ${C.accent}` }}>2 · which jobs suit them</span>
        <span style={{ color: C.faint }}>3 · how their work gets checked</span>
      </div>
      {/* the lead, outside the office */}
      <Badge x={760} y={110} w={400} name="Claude Opus 5.5" role="plans, designs, reviews · decides who does what" meta="hosted · drives Claude Code" color={C.ink} on lead />
      {/* the office */}
      <div style={{ position: "absolute", left: 140, top: 430, width: 1640, height: 470, borderRadius: 24, border: `2px dashed ${C.line}` }} />
      <Mono size={18} style={{ position: "absolute", left: 170, top: 445 }}>this Mac · nothing inside leaves it</Mono>
      <div style={{ position: "absolute", left: 860, top: 416, width: 200, height: 28, background: C.bg, border: `2px solid ${C.accent}`, borderRadius: 8,
                    display: "flex", alignItems: "center", justifyContent: "center" }}><Mono size={15} color={C.ink}>127.0.0.1:8090</Mono></div>
      {/* a hand-off: the lead passes a card through the door to the sorting intern */}
      <Arrow x1={960} y1={310} x2={960} y2={410} color={C.ink} width={3} />
      <Arrow x1={960} y1={450} x2={x0 + 2 * (deskW + gap) + deskW / 2} y2={deskY - 14} color={C.decision} width={3} bend={-30} />
      <div style={{ position: "absolute", left: 1000, top: 330, width: 360, padding: "10px 14px", borderRadius: 10, background: "#f1ece3", color: "#15130f",
                    fontFamily: BODY, fontSize: 18, transform: "rotate(-2deg)", boxShadow: "0 10px 30px rgba(0,0,0,0.5)" }}>
        Which emoji fits “when suddenly a White Rabbit”?
        <div style={{ fontFamily: MONO, fontSize: 13, marginTop: 4, color: "#55503f" }}>decide · 346 options</div>
      </div>
      <Mono size={18} color={C.decision} style={{ position: "absolute", left: 760, top: 520 }}>decide →</Mono>
      {INTERNS.map((it, i) => (
        <Badge key={it.name} x={x0 + i * (deskW + gap)} y={deskY} w={deskW} name={it.name} role={it.job}
               meta={`${it.gb} GB${it.speed ? " · " + it.speed : ""}`} color={it.color} on={it.on} />
      ))}
      {/* the budget: desks in use */}
      <div style={{ position: "absolute", left: x0, top: 860, display: "flex", alignItems: "center", gap: 14 }}>
        <Mono size={16}>desks in use</Mono>
        <div style={{ width: 400, height: 10, borderRadius: 5, background: C.line, overflow: "hidden" }}>
          <div style={{ width: `${(16 + 3) / 28 * 100}%`, height: "100%", background: C.ink }} />
        </div>
        <Mono size={16} color={C.ink}>19 of 28 GB · OpenJev needs 9.5: the longest-idle intern goes home</Mono>
      </div>
      {/* timesheet */}
      <div style={{ position: "absolute", right: 60, top: 120, width: 260, padding: 16, borderRadius: 12, border: `1px solid ${C.line}`, background: "rgba(0,0,0,0.25)" }}>
        <Mono size={14} color={C.faint}>TIMESHEET · this film</Mono>
        {[["lead", "—"], ["Gemma", "—"], ["Jev-Style", "—"], ["OpenJev", "—"], ["TTS", "—"], ["Whisper", "—"]].map(([a, b]) => (
          <div key={a} style={{ display: "flex", justifyContent: "space-between", marginTop: 6 }}><Mono size={15}>{a}</Mono><Mono size={15} color={C.ink}>{b}</Mono></div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
