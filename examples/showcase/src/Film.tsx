import React from "react";
import { AbsoluteFill, Audio, Sequence, interpolate, staticFile, useCurrentFrame } from "remotion";
import { Captions } from "./Captions";
import { AllScenesCtx, DataCtx, Props, SceneCtx, SceneT, sceneFrames } from "./lib";
import { C } from "./theme";
import { Catch, Cost_, Deal, Hook, Latency_, Privacy_ } from "./scenes/Act1";
import { Door, Team } from "./scenes/Act2";
import { Evidence, Mechanism, Sort } from "./scenes/Act3";
import { GatesLoop, Route, Wrong } from "./scenes/Gates";
import { Voice } from "./scenes/Voice";
import { Timesheet } from "./scenes/Timesheet";

const VIEWS: Record<string, React.FC> = { hook: Hook, deal: Deal, cost: Cost_, latency: Latency_, private: Privacy_, catch: Catch, team: Team,
                                          door: Door, sort: Sort, mechanism: Mechanism, evidence: Evidence, wrong: Wrong, gates: GatesLoop,
                                          route: Route, voice: Voice, timesheet: Timesheet };
export const XFADE = 12;      // scenes overlap by this many frames and cross-fade

export const filmFrames = (scenes: SceneT[]) => scenes.reduce((n, s) => n + sceneFrames(s), 0);

const Fade: React.FC<{ len: number; first: boolean; last: boolean; children: React.ReactNode }> = ({ len, first, last, children }) => {
  const f = useCurrentFrame();
  const op = Math.min(first ? 1 : interpolate(f, [0, XFADE], [0, 1], { extrapolateRight: "clamp" }),
                      last ? 1 : interpolate(f, [len - 1, len + XFADE - 1], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }));
  return <AbsoluteFill style={{ opacity: op }}>{children}</AbsoluteFill>;
};

export const Film: React.FC<Props> = ({ timeline, data }) => {
  let at = 0;
  const starts = timeline.scenes.map(s => { const a = at; at += sceneFrames(s); return a; });
  return (
    <DataCtx.Provider value={data}><AllScenesCtx.Provider value={timeline.scenes}>
      <AbsoluteFill style={{ background: C.bg, overflow: "hidden" }}>
        <AbsoluteFill style={{ background: `radial-gradient(ellipse 80% 70% at 50% 40%, ${C.bg2} 0%, ${C.bg} 70%)` }} />
        {timeline.scenes.map((s, i) => {
          const View = VIEWS[s.id];
          const len = sceneFrames(s);
          const last = i === timeline.scenes.length - 1;
          return (
            <Sequence key={s.id} from={starts[i]} durationInFrames={len + (last ? 0 : XFADE)} name={s.id}>
              <SceneCtx.Provider value={s}>
                <Fade len={len} first={i === 0} last={last}><View /></Fade>
                <Sequence from={Math.round(s.lead_s * 30)} name={`${s.id} audio`}><Audio src={staticFile(s.audio)} /></Sequence>
              </SceneCtx.Provider>
            </Sequence>
          );
        })}
        <Captions scenes={timeline.scenes} starts={starts} />
      </AbsoluteFill>
    </AllScenesCtx.Provider></DataCtx.Provider>
  );
};
