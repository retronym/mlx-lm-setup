import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Arrow, Body, Dot, Mono, along, prog, useCue, useData } from "../lib";
import { Box, Braces, Code } from "../parts";
import { C } from "../theme";
import { Readme } from "./Readme";

export const End: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const d = useData();
  const stop = cue("stop"), braces = cue("braces");
  const go = prog(f, 30, stop - 30, (t: number) => t);
  const fade = prog(f, braces - 20, 20);
  const msg = d.snippets.post.output.split("\n").find(l => l.includes("not included"))!.replace(/^\s*\|\s*/, "");
  const [x, y] = along(440, 680, 760, 680, go);
  return (
    <>
      <div style={{ position: "absolute", inset: 0, opacity: 1 - fade }}>
        <Readme x={80} y={100} w={760} glow={1} size={22} />
        <Appear at={12} dx={30} dy={0} style={{ left: 0, top: 0 }}>
          <Code code={d.snippets.post.code} x={900} y={100} w={940} size={21} title="the agent's program (snippets/post)" hl={{ 9: C.accent }} />
        </Appear>
        <Box x={120} y={600} w={320} h={160} title="Agent" sub="writes Scala" />
        <Box x={760} y={600} w={320} h={160} title="Compiler" sub="capture checking, safe mode" color={C.accent} lit={prog(f, stop, 12)} />
        <Box x={1300} y={600} w={320} h={160} title="REPL" sub="never reached" />
        <Arrow x1={440} y1={680} x2={760} y2={680} p={prog(f, 14, 16)} />
        <Arrow x1={1080} y1={680} x2={1300} y2={680} dash="8 10" p={0.5} />
        {go < 1 && <Dot x={x} y={y} />}
        <Appear at={stop + 4} style={{ left: 760, top: 790 }}>
          <Mono size={24} color={C.accent}>✕ {msg}</Mono>
        </Appear>
      </div>
      <Appear at={braces} dy={0} dur={24} style={{ left: 0, right: 0, top: 300, display: "flex", justifyContent: "center" }}>
        <Braces x={0} y={0} items={[]} size={170} arrow={false} style={{ position: "relative", alignItems: "center" }} />
      </Appear>
      <Appear at={braces + 30} style={{ left: 0, right: 0, top: 620, textAlign: "center" }}>
        <Body size={30} color={C.ink}>github.com/lampepfl/tacit · arXiv:2603.00991</Body>
        <Body size={22} color={C.muted} style={{ marginTop: 10 }}>{d.paper.authors.join(", ")}</Body>
        <Body size={20} color={C.faint} style={{ marginTop: 26 }}>Narration: synthetic voice · compiled with Scala {d.scala} on {d.measured.date}</Body>
      </Appear>
    </>
  );
};
