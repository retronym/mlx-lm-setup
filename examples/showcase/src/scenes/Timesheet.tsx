import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Heading, Mono, prog, sceneFrames, useAllScenes, useCue, useData, useScene } from "../lib";
import { C } from "../theme";

/** The film's own production jobs, split the same way: real counts from the build (draft.py, cards.py, narrate.py). */
export const Timesheet: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const { drafts, script_check, cards } = useData();
  const all = useAllScenes(), sc = useScene();
  const n = all.length;
  const pct = Math.round(100 * drafts.words_from_drafts / drafts.words);
  const rows: [string, string, string][] = [
    ["Gemma 4", `drafted all ${drafts.scenes} scenes`, `${drafts.llm_attempts} gated attempts`],
    ["OpenJev 4B", "fact-checked the drafts and the final script", `${drafts.nli_checks + script_check.sentences} sentences`],
    ["Jev-Style 2B", "sorted the job cards, answered the PR", `${cards.counts["jevstyle-2b"]} decisions`],
    ["Qwen3-TTS", "voiced every scene", `${n} clips`],
    ["Whisper", "timed every word, checked pronunciation", `${n} transcripts`],
  ];
  const lead: [string, string][] = [["the storyboard", "structure, story, what is true"], ["the animation code", "Remotion"],
                                    ["the script edit", `${pct}% of the final words came from the local drafts`]];
  const end = sceneFrames(sc);
  const outro = prog(f, end - 80, 30);
  const same = prog(f, cue("same"), 16);
  return (
    <>
      <div style={{ position: "absolute", inset: 0, opacity: 1 - outro }}>
        <Heading size={52} style={{ position: "absolute", left: 160, top: 90 }}>Timesheet: making this film</Heading>
        <div style={{ position: "absolute", left: 160, top: 200, width: 920 }}>
          <Mono size={20} color={C.good}>THE INTERNS · on this Mac</Mono>
          {rows.map(([who, what, count], i) => (
            <Appear key={who} at={cue("interns") + i * 10} style={{ position: "relative" }}>
              <div style={{ display: "flex", alignItems: "baseline", gap: 20, padding: "14px 0", borderBottom: `1px solid ${C.line}` }}>
                <div style={{ width: 210 }}><Heading size={30}>{who}</Heading></div>
                <div style={{ flex: 1, fontSize: 24, color: C.ink, fontFamily: "inherit" }}><Mono size={22} color={C.ink}>{what}</Mono></div>
                <Mono size={22} color={C.good}>{count}</Mono>
              </div>
            </Appear>
          ))}
        </div>
        <div style={{ position: "absolute", left: 1180, top: 200, width: 580 }}>
          <Appear at={cue("lead") - 6} style={{ position: "relative" }}><Mono size={20} color={C.ink}>THE LEAD · Claude Opus 5.5</Mono></Appear>
          {lead.map(([what, sub], i) => (
            <Appear key={what} at={cue("lead") + i * 10} style={{ position: "relative" }}>
              <div style={{ padding: "14px 0", borderBottom: `1px solid ${C.line}` }}>
                <Heading size={30}>{what}</Heading>
                <Mono size={18}>{sub}</Mono>
              </div>
            </Appear>
          ))}
        </div>
        <div style={{ position: "absolute", left: 160, top: 780, opacity: same }}><Heading size={60}>Same split, same reasons.</Heading></div>
      </div>
      <div style={{ position: "absolute", left: 0, right: 0, top: 420, textAlign: "center", opacity: outro }}>
        <Heading size={80}>mlx-lm-setup</Heading>
        <div style={{ marginTop: 18 }}><Mono size={30}>a lead, its interns, and one Mac · 127.0.0.1:8090</Mono></div>
      </div>
    </>
  );
};
