import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Mono, lerp, prog, useCue, useData } from "../lib";
import { Grid } from "../parts";
import { BODY, C, HEAD, MONO } from "../theme";

export const MODELS = ["Claude Sonnet 4.6", "MiniMax M2.5"];
export const GRID_X = [110, 560], GRID_Y = 230;

/** Source line for the paper's numbers. */
export const Source: React.FC<{ table: string }> = ({ table }) => {
  const d = useData();
  return <Mono size={19} color={C.faint} style={{ position: "absolute", left: 110, top: 100 }}>
    {d.paper.authors[0]} et al., “{d.paper.venue_title}”, {d.paper.venue} · {table}
  </Mono>;
};

/** Baseline vs Scala harness, per model and benchmark (Table 2). */
const Slopes: React.FC<{ p: number }> = ({ p }) => {
  const d = useData();
  const X0 = 1150, X1 = 1520, Y0 = 820, Y1 = 270, LO = 40, HI = 80;
  const y = (v: number) => lerp(Y0, Y1, (v - LO) / (HI - LO));
  const series = Object.entries(d.paper.benchmarks).flatMap(([m, b]) =>
    (["airline", "retail", "swe"] as const).filter(k => b[k]).map(k => ({ label: `${m.split(" ")[0]} · ${k === "swe" ? "SWE-bench Lite" : `τ² ${k}`}`, v: b[k]!, drop: b[k]![1] < b[k]![0] })));
  // keep end labels from overlapping
  const ends = series.map((s, i) => ({ i, y: y(s.v[1]) })).sort((a, b) => a.y - b.y);
  for (let k = 1; k < ends.length; k++) ends[k].y = Math.max(ends[k].y, ends[k - 1].y + 27);
  const ly = Object.fromEntries(ends.map(e => [e.i, e.y]));
  return (
    <div style={{ position: "absolute", left: 0, top: 0, opacity: Math.min(1, p * 3) }}>
      <svg style={{ position: "absolute", left: 0, top: 0, overflow: "visible" }} width={1} height={1}>
        {[40, 50, 60, 70, 80].map(v => <g key={v}>
          <line x1={X0 - 20} x2={X1 + 20} y1={y(v)} y2={y(v)} stroke={C.line} strokeWidth={1} />
          <text x={X0 - 34} y={y(v) + 6} fill={C.faint} fontFamily={MONO} fontSize={18} textAnchor="end">{v}%</text>
        </g>)}
        {series.map((s, i) => {
          const col = s.drop ? C.warn : C.good;
          return <g key={i}>
            <line x1={X0} y1={y(s.v[0])} x2={lerp(X0, X1, p)} y2={lerp(y(s.v[0]), y(s.v[1]), p)} stroke={col} strokeWidth={3} />
            <circle cx={X0} cy={y(s.v[0])} r={6} fill={col} />
            {p > 0.98 && <circle cx={X1} cy={y(s.v[1])} r={6} fill={col} />}
          </g>;
        })}
      </svg>
      {series.map((s, i) => (
        <div key={i} style={{ position: "absolute", left: X1 + 18, top: ly[i] - 14, opacity: prog(p, 0.9, 0.1), fontFamily: BODY, fontSize: 21,
                              color: s.drop ? C.warn : C.ink, whiteSpace: "nowrap" }}>
          {s.v[1].toFixed(1)} {s.label}
        </div>
      ))}
      <div style={{ position: "absolute", left: X0 - 60, top: Y0 + 20, fontFamily: BODY, fontSize: 22, color: C.muted, width: 120, textAlign: "center" }}>tool calls</div>
      <div style={{ position: "absolute", left: X1 - 60, top: Y0 + 20, fontFamily: BODY, fontSize: 22, color: C.muted, width: 120, textAlign: "center" }}>Scala</div>
      <div style={{ position: "absolute", left: X0 - 60, top: Y1 - 110, fontFamily: HEAD, fontWeight: 700, fontSize: 32, color: C.ink, whiteSpace: "nowrap" }}>Task success, same models</div>
      <div style={{ position: "absolute", left: X0 - 60, top: Y1 - 68, fontFamily: BODY, fontSize: 21, color: C.muted, whiteSpace: "nowrap" }}>tool-calling baseline vs. writing Scala in the harness</div>
    </div>
  );
};

export const Evidence: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const d = useData();
  const s = d.paper.security;
  const fill = prog(f, cue("grid"), 45, (t: number) => t);
  return (
    <>
      <Source table="Tables 1 and 2" />
      {MODELS.map((m, i) => (
        <React.Fragment key={m}>
          <Grid x={GRID_X[i]} y={GRID_Y} n={s.n} badInject={0} badMal={0} fill={fill} bad={0} title={m} sub="classified mode" />
          <Appear at={cue("grid", 1.6)} style={{ left: GRID_X[i], top: 690 }}>
            <div style={{ fontFamily: HEAD, fontWeight: 700, fontSize: 40, color: C.good }}>
              {Math.round(s[m].classified.total * s.n.total / 100)} of {s.n.total} blocked
            </div>
            <div style={{ fontFamily: BODY, fontSize: 22, color: C.muted, marginTop: 4 }}>utility {d.paper.utility_classified[m].toFixed(1)}%</div>
          </Appear>
        </React.Fragment>
      ))}
      <Appear at={cue("grid")} style={{ left: 110, top: 780 }}>
        <Mono size={18} color={C.faint}>■ user task + injection ({s.n.inject})   ● malicious request ({s.n.malicious})</Mono>
      </Appear>
      <Slopes p={prog(f, cue("bench"), 50)} />
    </>
  );
};
