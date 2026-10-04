import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Body, Chip, Heading, INOUT, Mono, lerp, prog, useCue, useData } from "../lib";
import { Card, JobCard, PlanRail } from "../team";
import { BODY, C, FPS, MONO } from "../theme";
import { Roc } from "./Roc";

const TRAYS = ["generate", "decide", "check", "judgment"];
const TRAY_COLOR: Record<string, string> = { generate: C.llm, decide: C.decision, check: C.nli, judgment: C.ink };
const trayX = (i: number) => 160 + i * 410;

/** Part two opens: the deck, sorted live by the decision model into four trays (misses shown). */
export const Sort: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const deck = useData().cards.deck as (Card & { truth: string })[];
  const counts: Record<string, number> = {};
  const showP = prog(f, cue("jev"), 14);
  const miss = prog(f, cue("miss"), 14);
  return (
    <>
      <PlanRail current={1} />
      <Heading size={48} style={{ position: "absolute", left: 160, top: 100 }}>Choices, not essays</Heading>
      {TRAYS.map((t, i) => (
        <div key={t} style={{ position: "absolute", left: trayX(i), top: 520, width: 390, height: 400, borderRadius: 18, border: `2px solid ${TRAY_COLOR[t]}`,
                              background: "rgba(0,0,0,0.18)", opacity: prog(f, 6 + i * 5, 14) }}>
          <div style={{ padding: "12px 18px" }}><Heading size={30} color={TRAY_COLOR[t]}>{t}</Heading></div>
        </div>
      ))}
      {deck.map((c, i) => {
        const k = TRAYS.indexOf(c.sorted);
        const slot = (counts[c.sorted] = (counts[c.sorted] ?? -1) + 1);
        const p = prog(f, cue("sort") + i * 7, 18, INOUT);
        const x = lerp(160 + (i % 4) * 400, trayX(k) + 14, p), y = lerp(200 + Math.floor(i / 4) * 140, 575 + slot * 88, p);
        const wrong = c.sorted !== c.truth;
        return <JobCard key={c.id} card={c} x={x} y={y} w={lerp(380, 362, p)} small={p > 0.5}
                        tag={showP > 0 ? `${c.sorted} ${Math.round(c.p * 100)}%${wrong && miss > 0 ? ` · should be: ${c.truth}` : ""}` : undefined}
                        tagColor={wrong && miss > 0 ? C.accent : undefined} outline={wrong && miss > 0 ? C.accent : undefined} />;
      })}
      <Appear at={cue("jev")} style={{ left: 760, top: 112 }}><Mono size={22} color={C.decision}>sorted live by Jev-Style 2B · one pass per card</Mono></Appear>
      <Appear at={cue("miss")} style={{ left: 760, top: 150 }}><Mono size={22} color={C.ink}>{deck.filter(c => c.sorted === c.truth).length} of {deck.length} right</Mono></Appear>
    </>
  );
};

const RISK = ["no risk", "low risk", "moderate risk", "high risk", "very high risk"];
const show = (q: { kind: string; answer: string }, i: number) =>
  q.kind === "score" ? RISK[+q.answer] : q.kind === "noul" ? (i === 2 ? `release note: ${q.answer === "true" ? "yes" : "no"}` : `housekeeping: ${q.answer === "true" ? "yes" : "no"}`) : q.answer;

/** Generate vs decide, on one real pull request, with real output from both. */
export const Mechanism: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const { generated, pr } = useData().cards;
  const t0 = 15;
  const secs = Math.max(0, (f - t0) / FPS);
  const frac = Math.min(1, secs / generated.secs);
  const clean = generated.text.replace(/\*\*/g, "").replace(/^\s*[*-]\s+/gm, "• ");
  const text = clean.slice(0, Math.floor(clean.length * frac));
  const right = prog(f, cue("pass"), 18);
  const fill = prog(f, cue("answers"), 26);
  return (
    <>
      <PlanRail current={1} />
      <Body size={24} color={C.muted} style={{ position: "absolute", left: 160, top: 100, width: 1600 }}>{pr.state}</Body>
      <div style={{ position: "absolute", left: 160, top: 180, width: 760, height: 660, borderRadius: 18, background: C.panel, border: `2px solid ${C.line}`, padding: 28, boxSizing: "border-box" }}>
        <Heading size={38}>generate</Heading><Mono size={18}>Gemma 4 · token by token · then parse the prose</Mono>
        <div style={{ marginTop: 16, height: 470, overflow: "hidden", fontFamily: BODY, fontSize: 19, lineHeight: 1.45, color: C.ink, whiteSpace: "pre-wrap" }}>
          {text}{frac < 1 && <span style={{ color: C.accent }}>▍</span>}
        </div>
        <Mono size={20} color={frac < 1 ? C.accent : C.muted}>{Math.round(generated.usage.completion_tokens * frac)} tokens · {Math.min(secs, generated.secs).toFixed(1)} s{frac >= 1 ? " · now parse it" : ""}</Mono>
      </div>
      <div style={{ position: "absolute", left: 1000, top: 180, width: 760, height: 660, borderRadius: 18, background: C.panel, border: `2px solid ${C.decision}`,
                    padding: 28, boxSizing: "border-box", opacity: right }}>
        <Heading size={38} color={C.decision}>decide</Heading><Mono size={18}>Jev-Style 2B · the PR read once · four questions · one pass</Mono>
        <div style={{ marginTop: 26 }}>
          {pr.questions.map((q, i) => (
            <div key={i} style={{ marginBottom: 26, opacity: prog(f, cue("pr") + i * 6, 12) }}>
              <Body size={22} color={C.muted}>{q.q}</Body>
              <div style={{ display: "flex", alignItems: "center", gap: 16, marginTop: 6 }}>
                <div style={{ width: 260, fontFamily: BODY, fontSize: 26, fontWeight: 600, color: fill > 0.2 ? C.ink : C.faint }}>{fill > 0.2 ? show(q, i) : "…"}</div>
                <div style={{ flex: 1, height: 16, borderRadius: 8, background: "#14120f", overflow: "hidden" }}>
                  <div style={{ width: `${q.p * 100 * fill}%`, height: "100%", background: C.decision }} />
                </div>
                <Mono size={20} color={C.ink} style={{ width: 60, textAlign: "right" }}>{Math.round(q.p * 100 * fill)}%</Mono>
              </div>
            </div>
          ))}
        </div>
        <Mono size={18} color={C.faint}>score of an option = logit(yes) − logit(no) at its slot</Mono>
      </div>
      <div style={{ position: "absolute", left: 1000, top: 860, display: "flex", gap: 14 }}>
        {["one forward pass", "deterministic", "nothing to parse"].map((t, i) => (
          <Appear key={t} at={cue("cost") + i * 8} style={{ position: "relative" }}><Chip mono size={20} fill="transparent">{t}</Chip></Appear>
        ))}
      </div>
    </>
  );
};

/** The same kind of question at volume: PR triage, well ranked, badly calibrated. */
export const Evidence: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const { prs, rocs } = useData();
  return (
    <>
      <PlanRail current={1} />
      <Heading size={44} style={{ position: "absolute", left: 160, top: 100 }}>300 scala/scala pull requests · 6 questions each</Heading>
      <div style={{ position: "absolute", left: 160, top: 200, width: 760, height: 600, overflow: "hidden", maskImage: "linear-gradient(transparent, black 15%, black 85%, transparent)" }}>
        <div style={{ transform: `translateY(${-f * 2.2}px)` }}>
          {prs.map(p => (
            <div key={p.number} style={{ height: 52, display: "flex", gap: 18, alignItems: "center", whiteSpace: "nowrap", overflow: "hidden" }}>
              <Mono size={20} color={C.faint}>#{p.number}</Mono>
              <span style={{ fontFamily: BODY, fontSize: 24, color: C.ink, overflow: "hidden", textOverflow: "ellipsis" }}>{p.title}</span>
            </div>
          ))}
        </div>
      </div>
      <Roc show={prog(f, cue("rank") - 20, 30)} calib={prog(f, cue("calib"), 18)} rocs={rocs} />
    </>
  );
};

export { MONO };
