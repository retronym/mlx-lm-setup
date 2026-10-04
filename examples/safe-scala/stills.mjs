// Render a still of every scene at each of its cue frames (and just after), into data/safe-scala/stills/, for layout checks.
//   node examples/safe-scala/stills.mjs [scene,...]
import { bundle } from "@remotion/bundler";
import { renderStill, selectComposition } from "@remotion/renderer";
import { mkdirSync, readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PUB = resolve(HERE, "../../data/safe-scala");
const OUT = `${PUB}/stills`;
mkdirSync(OUT, { recursive: true });
const only = process.argv[2]?.split(",");
const { scenes } = JSON.parse(readFileSync(`${PUB}/timeline.json`, "utf8"));

const serveUrl = await bundle({ entryPoint: resolve(HERE, "src/index.ts"), publicDir: PUB });
for (const s of scenes) {
  if (only && !only.includes(s.id)) continue;
  const comp = await selectComposition({ serveUrl, id: `scene-${s.id}` });
  const at = (t) => Math.min(Math.round((s.lead_s + t) * 30), comp.durationInFrames - 1);
  const frames = { start: at(0.5), ...Object.fromEntries(Object.entries(s.cues).flatMap(([k, t]) => [[k, at(t)], [`${k}+2s`, at(t + 2)]])), end: comp.durationInFrames - 1 };
  for (const [name, frame] of Object.entries(frames)) {
    const output = `${OUT}/${s.id}-${String(frame).padStart(4, "0")}-${name}.png`;
    await renderStill({ serveUrl, composition: comp, frame, output, scale: 0.5 });
  }
  console.log(s.id, Object.keys(frames).length, "stills");
}
