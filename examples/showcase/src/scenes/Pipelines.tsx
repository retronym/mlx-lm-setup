import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Body, Capture, Chip, Heading, INOUT, Mono, prog, useCue, useData } from "../lib";
import { BODY, C, MONO } from "../theme";

const STATIONS = [
  { label: "spaCy chunker", sub: "phrases of 4–12 words", x: 300 },
  { label: "Jev-Style 2B", sub: "decide: emoji, colour, mood", x: 760, color: C.decision },
  { label: "fuse", sub: "with Qwen3-Coder log P(emoji)", x: 1220, color: C.llm },
  { label: "book.html", sub: "ruby text, tint", x: 1650 },
];
const TINT: Record<string, string> = { red: "#e34948", orange: "#eb6834", yellow: "#d9a300", green: "#2e9e4f", blue: "#2a78d6", purple: "#7a5ad6",
                                       pink: "#e87ba4", brown: "#8a5a2e", black: "#555555", white: "#b8b4a8", grey: "#8a8a8a", gold: "#c9a227" };
const HYP = ["internal housekeeping", "needs release notes", "documentation", "library: collections", "REPL", "performance"];
const NICE: Record<string, string> = { internal: "internal", docs: "docs", collections: "collections", repl: "REPL", perf: "performance", release_notes: "release notes" };

/** Plain code around a scorer: the emoji book, then PR triage with honest calibration. */
export const Pipelines: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const { book_sample, prs, rocs } = useData();
  const conveyor = 1 - prog(f, cue("book") - 4, 16, INOUT);
  const book = prog(f, cue("book"), 18) * (1 - prog(f, cue("prs") - 10, 14, INOUT));
  const triage = prog(f, cue("prs"), 18);
  const roc = prog(f, cue("roc") - 10, 18);
  const calib = prog(f, cue("calib"), 18);

  const sample = book_sample.slice(8, 40);
  const speed = 6.2;                                         // px per frame along the belt
  const spacing = 330;
  return (
    <>
      {conveyor > 0 && (
        <div style={{ position: "absolute", inset: 0, opacity: conveyor }}>
          <Heading size={48} style={{ position: "absolute", left: 160, top: 96 }}>Plain code, with a scorer inside</Heading>
          <div style={{ position: "absolute", left: 0, right: 0, top: 560, height: 4, background: C.line }} />
          {STATIONS.map(s => (
            <div key={s.label} style={{ position: "absolute", left: s.x - 150, top: 640, width: 300, textAlign: "center" }}>
              <div style={{ width: 4, height: 60, background: s.color ?? C.line, margin: "-80px auto 16px" }} />
              <Chip color={s.color ?? C.line} size={26}>{s.label}</Chip>
              <div style={{ marginTop: 10 }}><Mono size={18}>{s.sub}</Mono></div>
            </div>
          ))}
          {sample.map((p, i) => {
            const x = 1700 + f * speed - i * spacing - 600;
            if (x < -400 || x > 2000) return null;
            const scored = x > STATIONS[1].x - 60;
            const tinted = x > STATIONS[3].x - 160;
            return (
              <div key={i} style={{ position: "absolute", left: x - 140, top: 460, width: 280, textAlign: "center" }}>
                <div style={{ height: 40, fontSize: 30, opacity: scored ? 1 : 0, transform: `translateY(${scored ? 0 : 10}px)` }}>{p.top.join("")}</div>
                <div style={{ fontFamily: "Georgia, serif", fontSize: 24, color: C.ink, padding: "6px 10px", borderRadius: 8, lineHeight: 1.25,
                              background: tinted && p.color in TINT ? `${TINT[p.color]}44` : "rgba(255,255,255,0.05)" }}>{p.text}</div>
              </div>
            );
          })}
        </div>
      )}
      {book > 0 && (
        <div style={{ position: "absolute", inset: 0, opacity: book }}>
          <Capture src="captures/book.png" label="127.0.0.1:8766/book" x={300} y={90 + (1 - book) * 40} w={1320} frame0={cue("book")} dur={150} push={0.25} focus={[0.78, 0.62]} />
        </div>
      )}
      {triage > 0 && (
        <div style={{ position: "absolute", inset: 0, opacity: triage }}>
          <Heading size={44} style={{ position: "absolute", left: 160, top: 96 }}>300 scala/scala pull requests · 6 questions each</Heading>
          {/* PR titles stream up the left column */}
          <div style={{ position: "absolute", left: 160, top: 190, width: 760, height: 560, overflow: "hidden",
                        maskImage: "linear-gradient(transparent, black 15%, black 85%, transparent)" }}>
            <div style={{ transform: `translateY(${-(f - cue("prs")) * 2.2}px)` }}>
              {prs.map(p => (
                <div key={p.number} style={{ height: 52, display: "flex", gap: 18, alignItems: "center", whiteSpace: "nowrap", overflow: "hidden" }}>
                  <Mono size={20} color={C.faint}>#{p.number}</Mono>
                  <span style={{ fontFamily: BODY, fontSize: 24, color: C.ink, overflow: "hidden", textOverflow: "ellipsis" }}>{p.title}</span>
                </div>
              ))}
            </div>
          </div>
          <div style={{ position: "absolute", left: 160, top: 790, display: "flex", gap: 12, flexWrap: "wrap", width: 800, opacity: 1 - roc * 0.6 }}>
            {HYP.map(h => <Chip key={h} size={20} color={C.nli}>{h}?</Chip>)}
          </div>
          <Roc show={roc} calib={calib} rocs={rocs} />
        </div>
      )}
    </>
  );
};

const Roc: React.FC<{ show: number; calib: number; rocs: ReturnType<typeof useData>["rocs"] }> = ({ show, calib, rocs }) => {
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
