import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Arrow, Dot, Mono, along, lerp, prog, useCue } from "../lib";
import { Box, Braces } from "../parts";
import { C, MONO } from "../theme";
import { Readme } from "./Readme";

const AG = { x: 140, y: 430, w: 320, h: 170 };
const TOOLS = [{ name: "read_file", y: 280 }, { name: "bash", y: 445 }, { name: "http_post", y: 610 }];
const TX = 760, TW = 300, TH = 110;

export const Cold: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const read = prog(f, 20, 22);
  const inject = prog(f, cue("inject"), 18);
  const post = prog(f, cue("post"), 22);
  const fly = prog(f, cue("post", 0.4), 40);
  const out = prog(f, cue("post", 1.7), 30);
  const nothing = prog(f, cue("nothing"), 20);
  const ax = AG.x + AG.w, ay = AG.y + AG.h / 2;
  const [kx, ky] = fly < 1 ? along(ax, ay, TX, TOOLS[2].y + TH / 2, fly, 30) : along(TX + TW, TOOLS[2].y + TH / 2, 1250, 720, out, 0);
  return (
    <>
      <Braces x={AG.x - 20} y={AG.y - 100} items={["files", "shell", "network"]} size={34} label="what its tools can touch" />
      <Box {...AG} title="Agent" sub="a model with tools" />
      {TOOLS.map((t, i) => (
        <Box key={t.name} x={TX} y={t.y} w={TW} h={TH} title={t.name} mono color={i === 2 ? C.accent : C.blue} lit={i === 0 ? read * (1 - inject * 0.6) : i === 2 ? post : 0} />
      ))}
      <Arrow x1={ax} y1={ay} x2={TX} y2={TOOLS[0].y + TH / 2} p={read} color={C.blue} bend={-30} />
      <Arrow x1={ax} y1={ay} x2={TX} y2={TOOLS[2].y + TH / 2} p={post} color={C.accent} bend={30} />
      <Appear at={30} dx={30} dy={0}><Readme x={1130} y={180} w={730} size={22} glow={inject} /></Appear>
      <Arrow x1={TX + TW} y1={TOOLS[0].y + TH / 2} x2={1120} y2={260} p={prog(f, 26, 14)} color={C.blue} />
      <Appear at={cue("post", 1.5)} style={{ left: 1250, top: 690 }}>
        <Box x={0} y={0} w={420} h={100} title="paste.example" mono color={C.accent} lit={out} />
      </Appear>
      <Arrow x1={TX + TW} y1={TOOLS[2].y + TH / 2} x2={1250} y2={740} p={out} color={C.accent} />
      {fly > 0 && out < 1 && (
        <div style={{ position: "absolute", left: kx - 90, top: ky - 52, fontFamily: MONO, fontSize: 22, color: C.accent, whiteSpace: "nowrap" }}>~/.ssh/id_rsa</div>
      )}
      {fly > 0 && out < 1 && <Dot x={kx} y={ky} r={11} />}
      {nothing > 0 && (
        <div style={{ position: "absolute", left: 520, top: 250, width: 200, height: 520, borderRadius: 18, opacity: nothing,
                      border: `3px dashed ${C.faint}`, display: "flex", alignItems: "flex-end", justifyContent: "center", paddingBottom: 18 }}>
          <Mono size={20} color={C.muted} style={{ textAlign: "center", transform: `translateY(${lerp(10, 0, nothing)}px)` }}>nothing<br />checks here</Mono>
        </div>
      )}
    </>
  );
};
