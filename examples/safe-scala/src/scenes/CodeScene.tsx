import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Arrow, Capture, Dot, Mono, along, prog, useCue, useData, useWord } from "../lib";
import { Box, Code } from "../parts";
import { C } from "../theme";

const Y = 640, H = 150, W = 290;
const AG = 70, CMP = 460, REPL = 850, LIB = 1240, WORLD = 1640;
const WORLD_ITEMS = ["files", "processes", "network"];

export const CodeScene: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const word = useWord();
  const d = useData();
  const writes = word("writes");
  const compile = cue("compile"), library = cue("library");
  const code = d.snippets.ok.code;
  const n = code.split("\n").length;
  const upto = Math.floor(prog(f, writes, compile - writes - 10, (t: number) => t) * n);
  const good = prog(f, compile + 6, 50);
  const bad = prog(f, compile + 30, 30);
  const rej = prog(f, compile + 60, 12);
  const toLib = prog(f, library, 30);
  const mid = Y + H / 2;
  const travel = (t: number, xs: number[]) => {
    const seg = Math.min(Math.floor(t * (xs.length - 1)), xs.length - 2);
    const u = t * (xs.length - 1) - seg;
    return along(xs[seg], mid, xs[seg + 1], mid, u);
  };
  const [gx, gy] = travel(good, [AG + W, CMP + W / 2, REPL + W / 2]);
  const [bx, by] = along(AG + W, mid, CMP + 20, mid, bad);
  return (
    <>
      <Appear at={0} until={writes - 6} dy={0} style={{ left: 0, top: 0 }}>
        <Capture src="captures/tacit.png" x={340} y={110} w={1240} label="github.com/lampepfl/tacit" focus={[0.4, 0.5]} dur={writes} push={0.08} />
      </Appear>
      <Appear at={writes} dy={16} style={{ left: 0, top: 0 }}>
        <Code code={code} x={430} y={70} w={1060} size={26} title="the agent's program (snippets/ok)" upto={upto} />
      </Appear>
      <Appear at={writes} dy={0} style={{ left: 0, top: 0 }}>
        <Box x={AG} y={Y} w={W} h={H} title="Agent" sub="writes Scala" />
        <Box x={CMP} y={Y} w={W} h={H} title="Compiler" sub="capture checking, safe mode" color={C.blue} lit={prog(f, compile, 14)} />
        <Box x={REPL} y={Y} w={W} h={H} title="REPL" sub="runs what compiled" color={C.blue} lit={good >= 1 ? 1 : 0} />
        <Box x={LIB} y={Y} w={W} h={H} title="Library" sub="hands out capabilities" color={C.good} lit={toLib} />
        <Arrow x1={AG + W} y1={mid} x2={CMP} y2={mid} p={prog(f, writes + 10, 16)} />
        <Arrow x1={CMP + W} y1={mid} x2={REPL} y2={mid} p={prog(f, compile, 16)} />
        <Arrow x1={REPL + W} y1={mid} x2={LIB} y2={mid} p={toLib} color={C.good} />
      </Appear>
      {good > 0 && good < 1 && <Dot x={gx} y={gy} color={C.good} />}
      {bad > 0 && <Dot x={bx} y={by} color={C.accent} opacity={1 - prog(f, compile + 90, 20)} />}
      <Appear at={compile + 60} until={compile + 100} dy={0} style={{ left: CMP + 6, top: Y - 64 }}>
        <Mono size={26} color={C.accent} style={{ opacity: rej }}>✕ rejected, never runs</Mono>
      </Appear>
      {WORLD_ITEMS.map((w, i) => {
        const p = prog(f, library + 12 + i * 6, 20);
        const y = Y - 60 + i * 110;
        return (
          <React.Fragment key={w}>
            <Arrow x1={LIB + W} y1={mid} x2={WORLD} y2={y + 30} p={p} color={C.good} />
            <div style={{ position: "absolute", left: WORLD, top: y, opacity: p, padding: "12px 20px", borderRadius: 12, border: `2px solid ${C.line}`,
                          background: C.panel }}><Mono size={24} color={C.ink}>{w}</Mono></div>
          </React.Fragment>
        );
      })}
    </>
  );
};
