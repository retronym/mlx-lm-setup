import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Body, Heading, Mono, useData } from "../lib";
import { C } from "../theme";




const NICE: Record<string, string> = { internal: "internal", docs: "docs", collections: "collections", repl: "REPL", perf: "performance", release_notes: "release notes" };

export const Roc: React.FC<{ show: number; calib: number; rocs: ReturnType<typeof useData>["rocs"] }> = ({ show, calib, rocs }) => {
  const S = 520, X = 1060, Y = 220;
  const hi = rocs.find(r => r.label === "collections")!;
  return (
    <div style={{ position: "absolute", left: X, top: Y, opacity: show }}>
      <div style={{ position: "absolute", left: 0, top: 0, width: S, height: S, borderLeft: `2px solid ${C.line}`, borderBottom: `2px solid ${C.line}` }} />
      <Mono size={18} style={{ position: "absolute", left: S - 220, top: S + 12, whiteSpace: "nowrap" }}>false positive rate →</Mono>
      <Mono size={18} style={{ position: "absolute", left: -40, top: S, transform: "rotate(-90deg)", transformOrigin: "0 0", whiteSpace: "nowrap" }}>true positive rate →</Mono>
      <svg style={{ position: "absolute", left: 0, top: 0, overflow: "visible" }} width={S} height={S}>
        <line x1={0} y1={S} x2={S} y2={0} stroke={C.line} strokeDasharray="6 8" strokeWidth={2} />
        {rocs.map(r => {
          const n = Math.max(2, Math.round(r.curve.length * show));
          const d = r.curve.slice(0, n).map(([x, y], i) => `${i ? "L" : "M"} ${x * S} ${S - y * S}`).join(" ");
          const isHi = r === hi;
          return <path key={r.label} d={d} fill="none" stroke={isHi ? C.nli : "#6f6380"} strokeWidth={isHi ? 5 : 2.5} opacity={isHi ? 1 : 0.7 - calib * 0.3} />;
        })}
        {calib > 0 && (
          <g opacity={calib}>
            <circle cx={hi.at_half[0] * S} cy={S - hi.at_half[1] * S} r={12} fill={C.accent} />
            <line x1={hi.at_half[0] * S} y1={S - hi.at_half[1] * S} x2={hi.at_half[0] * S + 60} y2={S + 56} stroke={C.accent} strokeWidth={2} />
          </g>
        )}
      </svg>
      <div style={{ position: "absolute", left: S + 30, top: 0, width: 280 }}>
        {rocs.map(r => (
          <div key={r.label} style={{ display: "flex", justifyContent: "space-between", height: 40, opacity: r === hi || calib === 0 ? 1 : 0.6 }}>
            <Mono size={20} color={r === hi ? C.ink : C.muted}>{NICE[r.label]}</Mono><Mono size={20} color={C.ink}>AUROC {r.auc.toFixed(2)}</Mono>
          </div>
        ))}
      </div>
      <div style={{ position: "absolute", left: -60, top: S + 60, width: 820, opacity: calib }}>
        <Body size={26}>“collections?” at the default 0.5: flags {hi.flagged_at_half}, of which {Math.round(hi.flagged_at_half * hi.precision_at_half)} are right</Body>
        <Heading size={36} color={C.accent} style={{ marginTop: 12 }}>well ranked, badly calibrated</Heading>
      </div>
      <Appear at={0} style={{ left: 0, top: -50, whiteSpace: "nowrap" }}><Mono size={20} color={C.ink}>OpenJev 4B, zero-shot, vs the labels maintainers applied</Mono></Appear>
    </div>
  );
};
