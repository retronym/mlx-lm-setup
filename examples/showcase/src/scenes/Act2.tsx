import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Arrow, Chip, Heading, INOUT, Mono, lerp, prog, useCue } from "../lib";
import { CHAPTERS, DOOR, LEAD, LeadBadge, Office, PlanRail, RAIL_Y, deskCenter, railX } from "../team";
import { C, MONO } from "../theme";

/** Meet the team, then the plan: three titles appear in the middle and rise into the rail. */
export const Team: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const title = 1 - prog(f, cue("lead") - 10, 14, INOUT);
  const plan = ["plan", "p2", "p3"].map(c => cue(c));
  const rise = prog(f, plan[2] + 30, 24, INOUT);
  const officeOp = 1 - prog(f, plan[0] - 10, 16) * 0.75 * (1 - rise);
  return (
    <>
      <div style={{ position: "absolute", left: 160, top: 160, opacity: title }}>
        <Heading size={72}>Not local <i>or</i> hosted.</Heading>
        <Heading size={72} color={C.muted}>How to split the work.</Heading>
      </div>
      <Appear at={cue("team")} until={cue("lead") - 10} style={{ left: 160, top: 420 }}><Heading size={56} color={C.accent}>Think of a team.</Heading></Appear>
      <div style={{ opacity: officeOp }}>
        <Appear at={cue("lead")} dy={-20} style={{ left: 0, top: 0, width: 1920, height: 1080 }}><LeadBadge /></Appear>
        {f >= cue("interns") && <Office appear={cue("interns")} />}
      </div>
      {plan.map((at, i) => {
        const a = prog(f, at, 14);
        if (a <= 0) return null;
        const x = lerp(160, railX(i), rise), y = lerp(320 + i * 110, RAIL_Y, rise);
        return (
          <div key={i} style={{ position: "absolute", left: x, top: y, opacity: a * (1 - rise), whiteSpace: "nowrap",
                                fontFamily: MONO, fontSize: lerp(46, 20, rise), color: C.ink }}>{i + 1} · {CHAPTERS[i]}</div>
        );
      })}
      {rise > 0 && <PlanRail current={0} opacity={rise} />}
    </>
  );
};

/** Part one: one door; who comes through it; interns come in when needed and go home when idle; limited desks. */
export const Door: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const glow = prog(f, cue("door"), 12) * (1 - prog(f, cue("door") + 60, 20));
  const inn = cue("in"), out = cue("out"), desks = cue("desks");
  // Gemma: comes in at "in" (2 s), goes home at "out". Desks: Gemma 16 + Jev-Style 3 in; OpenJev (9.5) needs a desk; Jev-Style (idle longest) leaves.
  const gemmaLoading = f >= inn && f < inn + 60;
  const gemmaIn = f >= inn + 60 && f < out + 10;
  const deskPhase = f >= desks;
  const jevOut = f >= desks + 70;
  const nliLoading = f >= desks + 85 && f < desks + 145;
  const nliIn = f >= desks + 145;
  const used = !deskPhase ? (gemmaIn ? 16 : 0) : 16 + (jevOut ? 0 : 3) + (nliIn ? 9.5 : 0);
  const clients = [
    { label: "browser", sub: "chat · admin", y: 260 },
    { label: "any OpenAI client", sub: "/v1", y: 340 },
  ];
  return (
    <>
      <PlanRail current={0} />
      <LeadBadge />
      <Office doorGlow={glow}
              lights={{ gemma: gemmaIn || deskPhase, jev: deskPhase && !jevOut, nli: nliIn }}
              loading={{ gemma: gemmaLoading, nli: nliLoading }}
              notes={{ gemma: gemmaLoading ? ["loading · ≈ 2 s", C.warn] : f >= out && !deskPhase ? ["idle → home, memory freed", C.muted] : undefined,
                       jev: deskPhase && f >= desks + 30 && !jevOut ? ["idle longest", C.accent] : jevOut ? ["went home", C.muted] : undefined,
                       nli: deskPhase && !nliIn ? ["needs a desk: 9.5 GB", C.warn] : undefined } as never} />
      {/* who comes through the door */}
      <Arrow x1={960} y1={300} x2={960} y2={DOOR.y - 6} p={prog(f, cue("clients"), 16)} color={C.ink} width={3} />
      <Appear at={cue("clients")} style={{ left: 980, top: 340 }}><Mono size={20} color={C.ink}>MCP</Mono></Appear>
      {clients.map((c, i) => {
        const p = prog(f, cue("clients") + 30 + i * 12, 16);
        return p > 0 ? (
          <React.Fragment key={c.label}>
            <div style={{ position: "absolute", left: 200, top: c.y, opacity: p }}><Chip size={22}>{c.label}</Chip> <Mono size={16} style={{ marginLeft: 10 }}>{c.sub}</Mono></div>
            <Arrow x1={560} y1={c.y + 22} x2={DOOR.x - 8} y2={DOOR.y + 14} p={p} color={C.muted} width={2} />
          </React.Fragment>
        ) : null;
      })}
      {/* hand-off to Gemma when it is called in */}
      {f >= inn - 10 && f < out && <Arrow x1={960} y1={DOOR.y + 34} x2={deskCenter("gemma")[0]} y2={deskCenter("gemma")[1] - 10} p={prog(f, inn - 10, 14)} color={C.llm} width={3} bend={-40} />}
      {/* the desks: the memory budget */}
      <div style={{ position: "absolute", left: 200, top: 860, display: "flex", alignItems: "center", gap: 14, opacity: prog(f, inn - 20, 16) }}>
        <Mono size={18}>desks in use</Mono>
        <div style={{ width: 560, height: 12, borderRadius: 6, background: C.line, overflow: "hidden" }}>
          <div style={{ width: `${used / 28 * 100}%`, height: "100%", background: C.ink }} />
        </div>
        <Mono size={18} color={C.ink}>{used.toFixed(1)} of 28 GB</Mono>
        <Mono size={18} color={C.faint}>(of the Mac's 48)</Mono>
      </div>
      {f >= desks && <Arrow x1={960} y1={DOOR.y + 34} x2={deskCenter("nli")[0]} y2={deskCenter("nli")[1] - 10} p={prog(f, desks + 4, 14)} color={C.nli} width={3} bend={40} />}
    </>
  );
};
export { LEAD };
