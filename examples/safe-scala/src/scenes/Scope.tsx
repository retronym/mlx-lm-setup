import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Capture, Mono, prog, useCue, useData, useWord } from "../lib";
import { Braces, Code, Compiler } from "../parts";
import { BODY, C } from "../theme";

/** The valet key: one metaphor, used once, then replaced by the literal code. */
const Key: React.FC<{ x: number; y: number }> = ({ x, y }) => (
  <svg style={{ position: "absolute", left: x, top: y }} width={420} height={180} viewBox="0 0 420 180">
    <circle cx={90} cy={90} r={70} fill="none" stroke={C.ink} strokeWidth={14} />
    <circle cx={90} cy={90} r={22} fill={C.ink} opacity={0.25} />
    <path d="M160 80 H400 V100 H160 Z" fill={C.ink} />
    <path d="M330 100 v34 h20 v-34 M370 100 v24 h20 v-24" fill={C.ink} stroke={C.ink} strokeWidth={4} />
  </svg>
);

const Grant: React.FC<{ label: string; ok: boolean; at: number }> = ({ label, ok, at }) => (
  <Appear at={at} dx={-20} dy={0} style={{ position: "relative" }}>
    <div style={{ display: "flex", alignItems: "center", gap: 18, fontFamily: BODY, fontSize: 40, whiteSpace: "nowrap", color: ok ? C.ink : C.muted }}>
      <span style={{ width: 44, color: ok ? C.good : C.accent, fontWeight: 700 }}>{ok ? "✓" : "✕"}</span>{label}
    </div>
  </Appear>
);

export const Scope: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const word = useWord();
  const d = useData();
  const code = cue("code"), escape = cue("escape"), safe = cue("safe"), bypass = cue("bypass");
  const back = prog(f, word("hand"), 24);
  const block: Record<number, string> = f >= code ? { 5: C.good, 6: C.good, 7: C.good } : {};
  const hl: Record<number, string> = f >= escape ? { 5: C.accent, 6: C.good, 7: C.good, 8: C.accent } : block;
  return (
    <>
      <Appear at={0} until={code - 8} dy={0} style={{ left: 0, top: 0 }}>
        <div style={{ position: "absolute", left: 260, top: 380, transform: `translate(${back * -120}px, ${back * 40}px) rotate(${back * -25}deg)`, opacity: 1 - back * 0.5 }}>
          <Key x={0} y={0} />
        </div>
        <div style={{ position: "absolute", left: 900, top: 330, display: "flex", flexDirection: "column", gap: 34 }}>
          <Grant label="opens the doors" ok at={word("starts") - 4} />
          <Grant label="starts the car" ok at={word("starts") + 6} />
          <Grant label="opens the boot" ok={false} at={word("boot") - 4} />
          <Grant label="handed back afterwards" ok at={word("hand")} />
        </div>
      </Appear>
      <Appear at={code} until={safe - 8} dy={16} style={{ left: 0, top: 0 }}>
        <Code code={d.snippets.escape.code} x={90} y={110} w={1090} size={28} hl={hl} title="snippets/escape" />
        <Braces x={1260} y={170} items={["fs"]} size={64} color={f >= escape ? C.accent : C.good} strike={prog(f, escape + 20, 16)}
                label={f >= escape ? "tries to leave the block" : "lives only inside the block"} />
      </Appear>
      <Appear at={escape + 8} until={safe - 8} dy={20} style={{ left: 0, top: 0 }}>
        <Compiler snip={d.snippets.escape} x={100} y={560} w={1720} size={21} maxLines={8} />
      </Appear>
      <Appear at={safe} dy={16} style={{ left: 0, top: 0 }}>
        <Capture src="captures/safe.png" x={70} y={110} w={980} label="nightly.scala-lang.org · Safe Mode" focus={[0.42, 0.2]} frame0={safe} dur={45} push={0.55} />
      </Appear>
      <Appear at={word("casts")} dy={16} style={{ left: 0, top: 0 }}>
        <Compiler snip={d.snippets.cast} x={1100} y={130} w={760} size={18} maxLines={5} />
      </Appear>
      <Appear at={bypass} dy={16} style={{ left: 0, top: 0 }}>
        <Compiler snip={d.snippets.bypass} x={1100} y={400} w={760} size={15} maxLines={4} />
        <Mono size={22} color={C.muted} style={{ position: "absolute", left: 1104, top: 620, whiteSpace: "nowrap" }}>the only door to files is the library's</Mono>
      </Appear>
    </>
  );
};
