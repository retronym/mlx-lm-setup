import React from "react";
import { useCurrentFrame } from "remotion";
import { Heading, INOUT, Mono, prog, typed, useCue } from "../lib";
import { RIBBON, RibbonBar } from "../Ribbon";
import { C } from "../theme";
import { ColdRibbon, coldBlocks } from "./Cold";

/** The overloaded ribbon lifts away and empties; the title types onto the calm bar. */
export const Title: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const blocks = coldBlocks(() => -100, () => -100);                       // all already dropped
  const lift = (i: number) => prog(f, 4 + (blocks.length - 1 - i) * 5, 18, INOUT);
  const title = "A private AI back end for one Mac.";
  const t0 = cue("title");
  const shown = typed(title, f, t0, 30);
  const sub = prog(f, t0 + 40, 20);
  const cursorOn = f < t0 + title.length * 1 + 60 && Math.floor(f / 15) % 2 === 0;
  return (
    <>
      {f < 60 ? <ColdRibbon frame={1000} blocks={blocks} lift={lift} /> : <RibbonBar />}
      <div style={{ position: "absolute", left: RIBBON.x, top: 330, width: 1600 }}>
        <Heading size={92}>{shown}<span style={{ color: C.accent, opacity: cursorOn ? 1 : 0 }}>▍</span></Heading>
        <div style={{ marginTop: 28, opacity: sub, transform: `translateY(${(1 - sub) * 12}px)` }}>
          <Mono size={28} color={C.muted}>open-weight models · Apple silicon · nothing leaves the machine</Mono>
        </div>
      </div>
    </>
  );
};
