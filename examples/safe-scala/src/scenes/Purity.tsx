import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Mono, useCue, useData } from "../lib";
import { Braces, Code, Compiler } from "../parts";
import { C, MONO } from "../theme";

export const Purity: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const d = useData();
  const classified = cue("classified"), leak = cue("leak"), error = cue("error");
  const sig = d.api.split("\n").find(l => l.includes("def map"))!.trim();
  const hl: Record<number, string> = f >= leak ? { 8: C.accent } : f >= classified ? { 6: C.good } : {};
  return (
    <>
      <Appear at={0} dy={16} style={{ left: 0, top: 0 }}>
        <Code code={d.snippets.leak.code} x={100} y={90} w={960} size={28} hl={hl} title="snippets/leak" />
      </Appear>
      <Appear at={classified} dy={16} style={{ left: 1130, top: 110 }}>
        <div style={{ fontFamily: MONO, fontSize: 30, color: C.ink, padding: "14px 24px", borderRadius: 12, border: `2px solid ${C.good}`, background: C.goodSoft, display: "inline-block" }}>
          Classified[String]
        </div>
        <Mono size={20} color={C.muted} style={{ display: "block", marginTop: 10 }}>a secret, sealed: prints as Classified(****)</Mono>
      </Appear>
      <Appear at={classified + 30} dy={16} style={{ left: 1130, top: 270 }}>
        <Mono size={22} color={C.muted}>{sig}</Mono>
        <Braces x={0} y={50} items={["any.rd"]} size={60} color={C.good} label="may read, but holds no capability" />
      </Appear>
      <Appear at={leak + 10} dy={16} style={{ left: 1130, top: 480 }}>
        <Braces x={0} y={0} items={["fs"]} size={60} color={C.accent} label="what this function holds" />
      </Appear>
      <Appear at={error} dy={20} style={{ left: 0, top: 0 }}>
        <Compiler snip={d.snippets.leak} x={100} y={650} w={1720} size={23} maxLines={6} />
      </Appear>
    </>
  );
};
