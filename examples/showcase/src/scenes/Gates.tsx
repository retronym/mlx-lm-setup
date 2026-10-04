import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Arrow, Body, Chip, Heading, INOUT, Mono, along, lerp, prog, useCue, useData } from "../lib";
import { Card, DOOR, InternId, JobCard, LEAD, LeadBadge, Office, PlanRail, deskCenter } from "../team";
import { C, MONO } from "../theme";

const CODE = ["def moving_avg(xs, n):", "    out = []", "    for i in range(len(xs)):", "        window = xs[i:i+n]", "        out.append(sum(window) / n)", "    return out"];
const GATES = ["JSON", "pattern", "length", "faithful (NLI)"];

/** Part three opens with a real intern mistake, and the fact-checker agreeing with it. */
export const Wrong: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  return (
    <>
      <PlanRail current={2} />
      <Heading size={48} style={{ position: "absolute", left: 160, top: 100 }}>An intern's mistake</Heading>
      <div style={{ position: "absolute", left: 160, top: 220, width: 760, padding: 30, borderRadius: 16, background: C.panel, border: `2px solid ${C.line}` }}>
        {CODE.map((l, i) => (
          <div key={i} style={{ fontFamily: MONO, fontSize: 26, lineHeight: 1.7, whiteSpace: "pre", color: C.ink, background: i === 3 && f > cue("not") ? C.accentSoft : undefined }}>{l}</div>
        ))}
      </div>
      <Appear at={cue("review")} style={{ left: 1000, top: 230, width: 760 }}>
        <Mono size={20}>Qwen3-Coder, asked to review it</Mono>
        <div style={{ marginTop: 10, padding: "22px 28px", borderRadius: 16, background: C.panel, border: `2px solid ${f > cue("not") ? C.accent : C.line}` }}>
          <Body size={30}>“<b>moving_avg</b> raises an <b>IndexError</b> when n is larger than the length of xs.”</Body>
        </div>
      </Appear>
      <Appear at={cue("not")} style={{ left: 1000, top: 470 }}><Chip color={C.accent} size={26}>✗ invented: slicing never raises</Chip></Appear>
      <Appear at={cue("nli")} style={{ left: 1000, top: 570, width: 760 }}>
        <Mono size={20}>OpenJev 4B, asked whether the code supports the claim</Mono>
        <div style={{ marginTop: 10, display: "flex", gap: 16, alignItems: "center" }}>
          <Chip color={C.nli} size={28}>entailment 0.72</Chip><Chip color={C.accent} size={26}>✗ agreed with it</Chip>
        </div>
      </Appear>
    </>
  );
};

/** Generated work goes through gates: try 1 fails faithfulness, is fed back, try 2 passes. Persistent failure goes to the lead. */
export const GatesLoop: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const g0 = 20;
  const LLM: [number, number] = [330, 560], OUT: [number, number] = [1700, 560];
  const gx = (i: number) => 760 + i * 230;
  const a1 = prog(f, g0, 70, INOUT);
  const fail = f >= g0 + 70;
  const back = prog(f, g0 + 82, 34, INOUT);
  const a2 = prog(f, g0 + 122, 70, INOUT);
  const cand = (t: number, endX: number) => [lerp(LLM[0] + 120, endX, t), LLM[1]];
  let cx = 0, cy = 0, cColor = C.ink, show = true;
  if (f < g0 + 82) { [cx, cy] = cand(a1, gx(3)); cColor = fail ? C.accent : C.ink; }
  else if (f < g0 + 122) { [cx, cy] = along(gx(3), LLM[1] + 40, LLM[0] + 60, LLM[1] + 40, back, 260); cColor = C.accent; }
  else { [cx, cy] = cand(a2, OUT[0]); cColor = a2 > 0.98 ? C.good : C.ink; show = f < g0 + 122 + 90; }
  const passed = (i: number) => (f < g0 + 122 ? (fail ? i < 3 : lerp(LLM[0] + 120, gx(3), a1) > gx(i)) : lerp(LLM[0] + 120, OUT[0], a2) > gx(i));
  return (
    <>
      <PlanRail current={2} />
      <Heading size={48} style={{ position: "absolute", left: 160, top: 100 }}>So intern work goes through gates</Heading>
      <Mono size={22} style={{ position: "absolute", left: 160, top: 166 }}>iterate: retry until every check a machine can verify passes</Mono>
      <div style={{ position: "absolute", left: LLM[0] - 150, top: LLM[1] - 28 }}><Chip size={26} color={C.llm}>Gemma 4</Chip></div>
      {GATES.map((g, i) => {
        const ok = passed(i), bad = i === 3 && fail && f < g0 + 172;
        return (
          <div key={g} style={{ position: "absolute", left: gx(i) - 4, top: LLM[1] - 90, width: 8, height: 180, borderRadius: 4, background: bad ? C.accent : ok ? C.good : C.line }}>
            <div style={{ position: "absolute", top: -50, left: -100, width: 208, textAlign: "center" }}>
              <Mono size={21} color={bad ? C.accent : ok ? C.good : C.muted}>{g} {bad ? "✗" : ok ? "✓" : ""}</Mono>
            </div>
          </div>
        );
      })}
      <Arrow x1={gx(3) - 10} y1={LLM[1] + 50} x2={LLM[0] + 40} y2={LLM[1] + 50} bend={260} color={C.accent} p={prog(f, g0 + 76, 20)} dash="10 10" />
      <Appear at={cue("retry")} style={{ left: 760, top: LLM[1] + 220 }}><Mono size={22} color={C.accent}>the failed check is fed back · try again</Mono></Appear>
      <div style={{ position: "absolute", left: OUT[0] - 40, top: LLM[1] - 30, opacity: prog(f, g0 + 190, 14) }}><Chip size={26} color={C.good}>accepted</Chip></div>
      <Appear at={cue("lead")} style={{ left: 1180, top: 860 }}><Chip size={24} color={C.ink}>still failing after its tries → the lead takes over</Chip></Appear>
      {show && (
        <div style={{ position: "absolute", left: cx - 50, top: cy - 30, width: 100, height: 60, borderRadius: 10, background: C.panel, border: `3px solid ${cColor}`,
                      display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Mono size={18} color={C.ink}>try {f < g0 + 122 ? 1 : 2}</Mono>
        </div>
      )}
    </>
  );
};

const ROUTE: Record<string, { to: InternId | "lead"; tool: string }> = {
  generate: { to: "gemma", tool: "chat · iterate" }, decide: { to: "jev", tool: "decide" }, check: { to: "nli", tool: "entail" }, judgment: { to: "lead", tool: "" },
};

/** The whole deck routes: judgment up to the lead, everything else down through the door to the right intern. */
export const Route: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const deck = useData().cards.deck as (Card & { truth: string })[];
  const go = (i: number) => prog(f, cue("down") + i * 6, 24, INOUT);
  const tools = prog(f, cue("tools"), 16);
  const per: Record<string, number> = {};
  const lit = { gemma: go(0) > 0.9, jev: go(1) > 0.9, nli: go(3) > 0.9 };
  return (
    <>
      <PlanRail current={2} />
      <LeadBadge />
      <Office lights={lit} />
      {deck.map((c, i) => {
        const r = ROUTE[c.truth];
        const k = (per[r.to] = (per[r.to] ?? -1) + 1);
        const [tx, ty] = r.to === "lead" ? [LEAD.x + LEAD.w + 30 + k * 24, 150 + k * 60] : [deskCenter(r.to)[0] - 120 + k * 16, deskCenter(r.to)[1] - 110 + k * 14];
        const p = go(i);
        const sx = 130 + (i % 2) * 260, sy = 110 + Math.floor(i / 2) * 78;
        // non-judgment cards pass through the door on the way down
        const [x, y] = r.to === "lead" ? [lerp(sx, tx, p), lerp(sy, ty, p)]
          : p < 0.5 ? [lerp(sx, DOOR.x, p * 2), lerp(sy, DOOR.y - 40, p * 2)] : [lerp(DOOR.x, tx, p * 2 - 1), lerp(DOOR.y - 40, ty, p * 2 - 1)];
        return <JobCard key={c.id} card={c} x={x} y={y} w={240} small scale={lerp(1, 0.85, p)} opacity={1 - 0.3 * p} />;
      })}
      {(["gemma", "jev", "nli"] as InternId[]).map(id => {
        const t = Object.values(ROUTE).find(r => r.to === id)!.tool;
        const [x] = deskCenter(id);
        return <div key={id} style={{ position: "absolute", left: x - 100, top: 500, width: 200, textAlign: "center", opacity: tools }}><Mono size={22} color={C.ink}>{t}</Mono></div>;
      })}
      <Appear at={cue("tools") + 20} style={{ left: 1200, top: 300 }}><Mono size={20} color={C.ink}>judgment stays with the lead</Mono></Appear>
    </>
  );
};
