import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Body, Chip, Heading, INOUT, Mono, lerp, prog, typed, useCue, useData } from "../lib";
import { BODY, C, FPS, MONO } from "../theme";

// The /jev page's first sample, scored live by Jev-Style 2B when the captures were taken (data/showcase/captures/jev.png).
const REVIEW = "The battery easily lasts two days and the screen is gorgeous, but the speaker is tinny and support took a week to reply.";
const DECIDED: [string, number][] = [["mixed", 0.529], ["negative", 0.228], ["positive", 0.156], ["neutral", 0.087]];
const GENERATED = "Looking at this review, the customer praises the battery life and the display, but criticises the speaker quality and the slow support response. Weighing these, I would say the overall sentiment is best described as mixed, although one could argue it leans slightly";
const TINT: Record<string, string> = { pink: "#e87ba4", green: "#2e9e4f", white: "#b8b4a8", grey: "#8a8a8a", red: "#e34948" };

const Panel: React.FC<{ x: number; title: string; sub: string; accent?: boolean; children: React.ReactNode }> = ({ x, title, sub, accent, children }) => (
  <div style={{ position: "absolute", left: x, top: 250, width: 760, height: 600, borderRadius: 18, background: C.panel,
                border: `2px solid ${accent ? C.decision : C.line}`, padding: 32, boxSizing: "border-box" }}>
    <Heading size={40} color={accent ? C.decision : C.ink}>{title}</Heading>
    <Mono size={19}>{sub}</Mono>
    <div style={{ marginTop: 22 }}>{children}</div>
  </div>
);

export const Decide: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const { phrase } = useData();
  const split = prog(f, cue("pulse") - 6, 20) * (1 - prog(f, cue("alice") - 14, 16, INOUT));
  const intro = 1 - prog(f, cue("pulse") - 20, 20, INOUT);

  // ---- part A: generate vs decide
  const genText = typed(GENERATED, f, cue("pulse") + 12, 24);
  const genTokens = Math.round(genText.length / 4.2);
  const pulseX = prog(f, cue("pulse") + 30, 22, INOUT);
  const fill = prog(f, cue("pulse") + 46, 24);

  // ---- part B: one Alice phrase, one pass, four questions
  const ctx = prog(f, cue("alice"), 18) * (1 - prog(f, cue("result") + 20, 16));
  const squash = prog(f, cue("state"), 24, INOUT);
  const slab = prog(f, cue("state") + 10, 20);
  const qs = [
    { q: "emoji?", n: 346 }, { q: "vibe colour?", n: 12 }, { q: "mood?", n: 10 }, { q: "sentiment?", n: 5 },
  ];
  const zoom = prog(f, cue("slots"), 24, INOUT);
  const result = prog(f, cue("result"), 24, INOUT);
  const partB = prog(f, cue("alice"), 16) * (1 - result);

  const zs = phrase.emoji.map(e => e.z);
  const zmax = Math.max(...zs), zmin = Math.min(0, ...zs);
  const tint = TINT[phrase.attrs.color.top] ?? C.accent;

  return (
    <>
      {/* title, big then out of the way */}
      <div style={{ position: "absolute", left: 160, top: lerp(380, 96, 1 - intro), opacity: f < cue("alice") ? 1 : 1 - prog(f, cue("alice"), 12) }}>
        <Heading size={lerp(110, 48, 1 - intro)}>Decide, <span style={{ color: C.decision }}>don't generate</span></Heading>
      </div>

      {split > 0 && (
        <div style={{ position: "absolute", inset: 0, opacity: split }}>
          <Body size={24} color={C.muted} style={{ position: "absolute", left: 160, top: 176, width: 1600 }}>“{REVIEW}” · <i>What is the sentiment?</i></Body>
          <Panel x={160} title="generate" sub="token by token, then parse the prose">
            <div style={{ fontFamily: MONO, fontSize: 21, color: C.ink, lineHeight: 1.5, height: 360, overflow: "hidden" }}>
              {genText}<span style={{ color: C.accent }}>▍</span>
            </div>
            <Mono size={22} color={C.accent}>{genTokens} tokens · {((f - cue("pulse") - 12) / FPS).toFixed(1)} s · still going</Mono>
          </Panel>
          <Panel x={1000} title="decide" sub="score a closed list, one forward pass" accent>
            <div style={{ position: "relative" }}>
              {DECIDED.map(([o, p], i) => (
                <div key={o} style={{ display: "flex", alignItems: "center", gap: 18, height: 74 }}>
                  <div style={{ width: 150, fontFamily: BODY, fontSize: 30, color: C.ink, fontWeight: i === 0 ? 600 : 400 }}>{o}</div>
                  <div style={{ flex: 1, height: 22, borderRadius: 11, background: "#14120f", overflow: "hidden" }}>
                    <div style={{ width: `${p * 100 * fill / 0.6}%`, height: "100%", background: i === 0 ? C.decision : "#3f6f5d", borderRadius: 11 }} />
                  </div>
                  <Mono size={24} color={C.ink} style={{ width: 90, textAlign: "right" }}>{(p * 100 * fill).toFixed(1)}%</Mono>
                </div>
              ))}
              {pulseX > 0 && pulseX < 1 && (
                <div style={{ position: "absolute", top: -10, bottom: -10, left: `${pulseX * 100}%`, width: 90, transform: "translateX(-50%)",
                              background: `linear-gradient(90deg, transparent, ${C.decision}66, transparent)` }} />
              )}
            </div>
            <div style={{ marginTop: 26, opacity: fill }}><Mono size={22} color={C.decision}>done · 1 pass · probabilities for every option</Mono></div>
          </Panel>
        </div>
      )}

      {/* part B */}
      {partB > 0 && (
        <div style={{ position: "absolute", inset: 0, opacity: partB }}>
          <Mono size={22} style={{ position: "absolute", left: 160, top: 120 }}>Alice's Adventures in Wonderland, chapter I</Mono>
          {/* the context, then squashed into the state slab */}
          <div style={{ position: "absolute", left: 160, top: 200, width: 1600, opacity: ctx * (1 - squash), transform: `scaleY(${1 - squash * 0.9})`, transformOrigin: "50% 100%" }}>
            <div style={{ fontFamily: "Georgia, serif", fontSize: 44, lineHeight: 1.45, color: C.muted }}>
              … {phrase.before}{" "}
              <span style={{ color: C.ink, background: "rgba(255,255,255,0.08)", borderRadius: 6, padding: "0 8px", boxShadow: `inset 0 -3px 0 ${C.accent}` }}>{phrase.text}</span>{" "}
              {phrase.after} …
            </div>
            <Mono size={20} style={{ display: "block", marginTop: 14 }}>15 words before · the phrase · 5 words after ≈ 25 tokens</Mono>
          </div>
          {slab > 0 && (
            <div style={{ position: "absolute", left: 160, top: 300, width: 1600, height: 74, borderRadius: 12, opacity: slab * (1 - zoom * 0.5),
                          background: `linear-gradient(90deg, ${C.decision}55, ${C.decision}22)`, border: `2px solid ${C.decision}`, boxShadow: `0 0 50px ${C.decision}33`,
                          display: "flex", alignItems: "center", padding: "0 26px", boxSizing: "border-box", gap: 30 }}>
              <Mono size={24} color={C.ink}>state</Mono><Mono size={20}>“{phrase.text}” in context · computed once</Mono>
            </div>
          )}
          {qs.map((q, i) => {
            const a = prog(f, cue("questions") + i * 8, 16);
            const y = 430 + i * 100;
            const other = i === 0 ? 0 : zoom;
            return a > 0 ? (
              <div key={q.q} style={{ position: "absolute", left: 160, top: y, width: 1600, height: 70, opacity: a * (1 - other) * (i === 0 ? 1 - zoom : 1) }}>
                <svg style={{ position: "absolute", left: 0, top: -56 - i * 100 + 0 }} width={1600} height={56 + i * 100}>
                  <line x1={260} y1={0} x2={260} y2={56 + i * 100} stroke={C.decision} strokeWidth={2} opacity={0.35} />
                </svg>
                <div style={{ position: "absolute", left: 0, top: 14, width: 360 }}>
                  <span style={{ fontFamily: BODY, fontSize: 30, color: C.ink }}>{q.q}</span>{" "}<Mono size={22}>{q.n} options</Mono>
                </div>
                <div style={{ position: "absolute", left: 400, top: 10, width: 1200, height: 50, display: "flex", gap: q.n > 50 ? 1.5 : 8 }}>
                  {Array.from({ length: q.n }, (_, k) => (
                    <div key={k} style={{ flex: 1, background: C.decision, opacity: 0.25 + 0.6 * prog(f, cue("questions") + i * 8 + (k / q.n) * 20, 6), borderRadius: 2 }} />
                  ))}
                </div>
              </div>
            ) : null;
          })}

          {/* zoom into the emoji question: how one option is scored, then the real scores */}
          {zoom > 0 && (
            <div style={{ position: "absolute", left: 160, top: 470, width: 1600, opacity: zoom }}>
              <div style={{ display: "flex", alignItems: "flex-end", gap: 34 }}>
                <div style={{ width: 520 }}>
                  <Body size={30}>each option has a slot; its score is</Body>
                  <div style={{ fontFamily: MONO, fontSize: 34, color: C.ink, marginTop: 16 }}>
                    logit(<span style={{ color: C.decision }}>yes</span>) − logit(<span style={{ color: C.muted }}>no</span>)
                  </div>
                  <Body size={24} color={C.muted} style={{ marginTop: 16 }}>read at that slot, all options in the same pass</Body>
                </div>
                <div style={{ flex: 1, display: "flex", alignItems: "flex-end", gap: 18, height: 330 }}>
                  {phrase.emoji.map((e, k) => {
                    const h = ((e.z - zmin) / (zmax - zmin)) * 250 * prog(f, cue("slots") + 30 + k * 3, 20);
                    return (
                      <div key={k} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 8 }}>
                        <Mono size={18} color={k === 0 ? C.ink : C.muted}>{e.z.toFixed(1)}</Mono>
                        <div style={{ width: "70%", height: h, borderRadius: 6, background: k === 0 ? C.decision : "#3f6f5d" }} />
                        <div style={{ fontSize: 46 }}>{e.e}</div>
                        <Mono size={15} style={{ textAlign: "center", height: 40 }}>{e.name}</Mono>
                      </div>
                    );
                  })}
                </div>
              </div>
              <Mono size={18} color={C.faint} style={{ display: "block", textAlign: "right", marginTop: 8 }}>emoji scores (z, fused with the LLM's), top 8 of 346</Mono>
            </div>
          )}
        </div>
      )}

      {/* the result: the annotated phrase */}
      {result > 0 && (
        <div style={{ position: "absolute", inset: 0, opacity: result }}>
          <div style={{ position: "absolute", left: 0, right: 0, top: 240, textAlign: "center" }}>
            <div style={{ display: "inline-flex", flexDirection: "column", alignItems: "center", gap: 10 }}>
              <div style={{ fontSize: 60, letterSpacing: 10, opacity: prog(f, cue("result") + 10, 20), transform: `translateY(${(1 - prog(f, cue("result") + 10, 20)) * 40}px)` }}>
                {phrase.top.map(t => t.e).join("")}
              </div>
              <div style={{ fontFamily: "Georgia, serif", fontSize: 76, color: C.ink, background: `${tint}55`, borderRadius: 10, padding: "0 16px" }}>{phrase.text}</div>
            </div>
          </div>
          <div style={{ position: "absolute", left: 0, right: 0, top: 520, display: "flex", justifyContent: "center", gap: 24 }}>
            {[
              <><span style={{ width: 18, height: 18, borderRadius: 9, background: tint, display: "inline-block" }} /> colour: {phrase.attrs.color.top}</>,
              <>mood: {phrase.attrs.mood.top} {(phrase.attrs.mood.p * 100).toFixed(0)}%</>,
              <>sentiment {phrase.attrs.sentiment.score >= 0 ? "+" : ""}{phrase.attrs.sentiment.score.toFixed(2)}</>,
            ].map((c, i) => (
              <Appear key={i} at={cue("result") + 24 + i * 8} style={{ position: "relative" }}><Chip size={28} color={C.decision}>{c}</Chip></Appear>
            ))}
          </div>
          <div style={{ position: "absolute", left: 0, right: 0, top: 700, display: "flex", justifyContent: "center", gap: 20 }}>
            {["1 forward pass", "≈ 1 s per phrase", "deterministic", "nothing to parse"].map((t, i) => (
              <Appear key={t} at={cue("cost") + i * 10} style={{ position: "relative" }}><Chip mono size={24} color={C.line} fill="transparent">{t}</Chip></Appear>
            ))}
          </div>
        </div>
      )}
    </>
  );
};
