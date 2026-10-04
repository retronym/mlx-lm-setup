import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Arrow, Body, Chip, Heading, INOUT, Mono, along, lerp, prog, useCue } from "../lib";
import { BODY, C, MONO } from "../theme";

const CODE = ["def moving_avg(xs, n):", "    out = []", "    for i in range(len(xs)):", "        window = xs[i:i+n]", "        out.append(sum(window) / n)", "    return out"];
const GATES = ["JSON", "regex", "length", "faithful (NLI)"];

/** Local models are confidently wrong; so their work goes through mechanical gates, and Claude keeps judgment. */
export const Gates: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const part1 = 1 - prog(f, cue("gate") - 8, 16, INOUT);
  const loop = prog(f, cue("gate"), 18) * (1 - prog(f, cue("split") - 10, 14, INOUT));
  const split = prog(f, cue("split"), 18);

  // the retry loop: attempt 1 fails the faithfulness gate and is fed back; attempt 2 passes
  const g0 = cue("gate") + 24;
  const LLM: [number, number] = [330, 560], OUT: [number, number] = [1700, 560];
  const gx = (i: number) => 760 + i * 230;
  const a1 = prog(f, g0, 70, INOUT);                     // travel to the 4th gate
  const fail = f >= g0 + 70;
  const back = prog(f, g0 + 82, 34, INOUT);              // fed back along the lower arc
  const a2 = prog(f, g0 + 122, 70, INOUT);               // second attempt, all the way out
  const cand = (t: number, endX: number) => [lerp(LLM[0] + 120, endX, t), LLM[1]];
  let cx = 0, cy = 0, cColor = C.ink, show = true;
  if (f < g0 + 82) { [cx, cy] = cand(a1, gx(3)); cColor = fail ? C.accent : C.ink; }
  else if (f < g0 + 122) { [cx, cy] = along(gx(3), LLM[1] + 40, LLM[0] + 60, LLM[1] + 40, back, 260); cColor = C.accent; }
  else { [cx, cy] = cand(a2, OUT[0]); cColor = a2 > 0.98 ? C.good : C.ink; show = f < g0 + 122 + 90; }
  const passed = (i: number) => (f < g0 + 122 ? (fail ? i < 3 : lerp(LLM[0] + 120, gx(3), a1) > gx(i)) : lerp(LLM[0] + 120, OUT[0], a2) > gx(i));

  return (
    <>
      {part1 > 0 && (
        <div style={{ position: "absolute", inset: 0, opacity: part1 }}>
          <Heading size={48} style={{ position: "absolute", left: 160, top: 96 }}>Confidently wrong</Heading>
          <div style={{ position: "absolute", left: 160, top: 220, width: 760, padding: 30, borderRadius: 16, background: C.panel, border: `2px solid ${C.line}` }}>
            {CODE.map((l, i) => (
              <div key={i} style={{ fontFamily: MONO, fontSize: 26, lineHeight: 1.7, whiteSpace: "pre", color: C.ink,
                                    background: i === 3 && f > cue("wrong") ? C.accentSoft : undefined }}>{l}</div>
            ))}
          </div>
          <Appear at={24} style={{ left: 1000, top: 230, width: 760 }}>
            <Mono size={20}>Qwen3-Coder, asked to review it</Mono>
            <div style={{ marginTop: 10, padding: "22px 28px", borderRadius: 16, background: C.panel, border: `2px solid ${f > cue("wrong") ? C.accent : C.line}` }}>
              <Body size={30}>“<b>moving_avg</b> raises an <b>IndexError</b> when n is larger than the length of xs.”</Body>
            </div>
          </Appear>
          <Appear at={cue("wrong") + 6} style={{ left: 1000, top: 470 }}>
            <Chip color={C.accent} size={26}>✗ invented: slicing never raises</Chip>
          </Appear>
          <Appear at={cue("nli")} style={{ left: 1000, top: 570, width: 760 }}>
            <Mono size={20}>OpenJev 4B, asked whether the code supports the claim</Mono>
            <div style={{ marginTop: 10, display: "flex", gap: 16, alignItems: "center" }}>
              <Chip color={C.nli} size={28}>entailment 0.72</Chip><Chip color={C.accent} size={26}>✗ agreed with it</Chip>
            </div>
          </Appear>
          <Appear at={cue("nli") + 40} style={{ left: 160, top: 640, width: 760 }}>
            <Body size={24} color={C.muted}>The real bug: the last n−1 windows are partial but still divided by n.</Body>
          </Appear>
        </div>
      )}

      {loop > 0 && (
        <div style={{ position: "absolute", inset: 0, opacity: loop }}>
          <Heading size={48} style={{ position: "absolute", left: 160, top: 96 }}>So local work goes through gates</Heading>
          <Mono size={22} style={{ position: "absolute", left: 160, top: 160 }}>iterate: retry until every check a machine can verify passes</Mono>
          <div style={{ position: "absolute", left: LLM[0] - 150, top: LLM[1] - 50 }}><Chip size={28} color={C.llm}>local LLM</Chip></div>
          {GATES.map((g, i) => {
            const ok = passed(i), bad = i === 3 && fail && f < g0 + 122 + 50;
            return (
              <div key={g} style={{ position: "absolute", left: gx(i) - 4, top: LLM[1] - 90, width: 8, height: 180, borderRadius: 4,
                                    background: bad ? C.accent : ok ? C.good : C.line }}>
                <div style={{ position: "absolute", top: -50, left: -100, width: 208, textAlign: "center" }}>
                  <Mono size={21} color={bad ? C.accent : ok ? C.good : C.muted}>{g} {bad ? "✗" : ok ? "✓" : ""}</Mono>
                </div>
              </div>
            );
          })}
          <Arrow x1={gx(3) - 10} y1={LLM[1] + 50} x2={LLM[0] + 40} y2={LLM[1] + 50} bend={260} color={C.accent} p={prog(f, g0 + 76, 20)} dash="10 10" />
          <Appear at={g0 + 84} until={g0 + 140} style={{ left: 760, top: LLM[1] + 220 }}>
            <Mono size={22} color={C.accent}>the failed check is fed back, and it tries again</Mono>
          </Appear>
          <div style={{ position: "absolute", left: OUT[0] - 40, top: LLM[1] - 30, opacity: prog(f, g0 + 190, 14) }}>
            <Chip size={26} color={C.good}>accepted</Chip>
          </div>
          {show && (
            <div style={{ position: "absolute", left: cx - 50, top: cy - 30, width: 100, height: 60, borderRadius: 10, background: C.panel,
                          border: `3px solid ${cColor}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
              <Mono size={18} color={C.ink}>try {f < g0 + 122 ? 1 : 2}</Mono>
            </div>
          )}
        </div>
      )}

      {split > 0 && (
        <div style={{ position: "absolute", inset: 0, opacity: split }}>
          <Heading size={48} style={{ position: "absolute", left: 160, top: 96 }}>A division of labour</Heading>
          {[
            { x: 160, title: "Claude", sub: "hosted · the big model", items: ["design", "review", "judgment", "hard reasoning"], color: C.ink },
            { x: 1200, title: "this Mac", sub: "local · cheap, bounded, checkable", items: ["summaries", "extraction", "classification", "boilerplate", "first-pass triage"], color: C.decision },
          ].map((col, i) => (
            <div key={col.title} style={{ position: "absolute", left: col.x, top: 230, width: 560, opacity: prog(f, cue("split") + i * 12, 16) }}>
              <Heading size={56} color={col.color}>{col.title}</Heading>
              <Mono size={20}>{col.sub}</Mono>
              <div style={{ marginTop: 26, display: "flex", flexDirection: "column", gap: 14 }}>
                {col.items.map((it, k) => (
                  <Appear key={it} at={cue("split") + 20 + i * 12 + k * 6} dx={i ? 20 : -20} dy={0} style={{ position: "relative" }}>
                    <div style={{ fontFamily: BODY, fontSize: 32, color: C.ink }}>{it}</div>
                  </Appear>
                ))}
              </div>
            </div>
          ))}
          {["chat", "decide", "entail", "iterate"].map((t, k) => {
            const y = 380 + k * 90;
            const p = prog(f, cue("split") + 40 + k * 8, 20);
            return (
              <React.Fragment key={t}>
                <Arrow x1={760} y1={y} x2={1160} y2={y} p={p} color={C.muted} width={3} />
                <div style={{ position: "absolute", left: 860, top: y - 40, width: 200, textAlign: "center", opacity: p }}><Mono size={22} color={C.ink}>{t}</Mono></div>
              </React.Fragment>
            );
          })}
          <Mono size={20} style={{ position: "absolute", left: 800, top: 760, width: 320, textAlign: "center", opacity: prog(f, cue("split") + 80, 16) }}>MCP tools</Mono>
        </div>
      )}
    </>
  );
};
