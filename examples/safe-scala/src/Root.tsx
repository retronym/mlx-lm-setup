import React from "react";
import { CalculateMetadataFunction, Composition, staticFile } from "remotion";
import { Film, filmFrames } from "./Film";
import { Props } from "./lib";
import { FPS, H, W } from "./theme";

const load: CalculateMetadataFunction<Props> = async () => {
  const [timeline, data] = await Promise.all(["timeline.json", "data.json"].map(f => fetch(staticFile(f)).then(r => r.json())));
  return { durationInFrames: filmFrames(timeline.scenes), props: { timeline, data } };
};

// One composition per scene too, for checking a scene on its own (`npm run still -- scene-purity out.png --frame 300`).
const SCENES = ["cold", "title", "code", "scope", "purity", "evidence", "limits", "end"];
const loadScene = (id: string): CalculateMetadataFunction<Props> => async () => {
  const [timeline, data] = await Promise.all(["timeline.json", "data.json"].map(f => fetch(staticFile(f)).then(r => r.json())));
  const scenes = timeline.scenes.filter((s: { id: string }) => s.id === id);
  return { durationInFrames: filmFrames(scenes), props: { timeline: { ...timeline, scenes }, data } };
};

export const Root: React.FC = () => (
  <>
    <Composition id="SafeScala" component={Film} width={W} height={H} fps={FPS} durationInFrames={1}
                 defaultProps={{ timeline: { fps: FPS, scenes: [] }, data: null as never }} calculateMetadata={load} />
    {SCENES.map(id => (
      <Composition key={id} id={`scene-${id}`} component={Film} width={W} height={H} fps={FPS} durationInFrames={1}
                   defaultProps={{ timeline: { fps: FPS, scenes: [] }, data: null as never }} calculateMetadata={loadScene(id)} />
    ))}
  </>
);
