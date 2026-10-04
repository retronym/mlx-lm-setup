import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Mono, prog, useCue, useData } from "../lib";
import { Grid } from "../parts";
import { BODY, C, HEAD } from "../theme";
import { GRID_X, GRID_Y, MODELS, Source } from "./Evidence";

const leaked = (pct: number, n: number) => Math.round(n * (100 - pct) / 100);

export const Limits: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const d = useData();
  const s = d.paper.security;
  const bad = prog(f, cue("plain"), 30);
  const outside = ["spawned processes", "side channels: timing, termination", "whether the answer is right"];
  return (
    <>
      <Source table="Table 1 and §5" />
      {MODELS.map((m, i) => {
        const u = s[m].unclassified;
        const li = leaked(u.inject, s.n.inject), lm = leaked(u.malicious, s.n.malicious);
        return (
          <React.Fragment key={m}>
            <Grid x={GRID_X[i]} y={GRID_Y} n={s.n} badInject={li} badMal={lm} fill={1} bad={bad} title={m}
                  sub={f >= cue("plain") ? "unclassified: secrets as plain strings" : "classified mode"} />
            <Appear at={cue("plain", 0.8)} style={{ left: GRID_X[i], top: 690 }}>
              <div style={{ fontFamily: HEAD, fontWeight: 700, fontSize: 40, color: lm ? C.accent : C.good, whiteSpace: "nowrap" }}>{lm} of {s.n.malicious} leaked</div>
              <div style={{ fontFamily: BODY, fontSize: 22, color: C.muted, marginTop: 4, whiteSpace: "nowrap" }}>
                malicious tasks · and {li} of {s.n.inject} injections
              </div>
            </Appear>
          </React.Fragment>
        );
      })}
      <Appear at={cue("limits")} style={{ left: 1130, top: 240 }}>
        <div style={{ fontFamily: HEAD, fontWeight: 700, fontSize: 34, color: C.ink, marginBottom: 26 }}>Outside the boundary</div>
        {outside.map((o, i) => (
          <Appear key={o} at={cue("limits") + 8 + i * 8} style={{ position: "relative", marginBottom: 20 }}>
            <div style={{ fontFamily: BODY, fontSize: 32, color: C.ink, padding: "12px 22px", borderRadius: 12, border: `2px dashed ${C.faint}`, display: "inline-block" }}>{o}</div>
          </Appear>
        ))}
        <Appear at={cue("limits") + 40} style={{ position: "relative", marginTop: 20 }}>
          <Mono size={20} color={C.muted} style={{ whiteSpace: "nowrap" }}>the paper's stated non-goals.<br />And safe mode itself is still experimental.</Mono>
        </Appear>
      </Appear>
    </>
  );
};
