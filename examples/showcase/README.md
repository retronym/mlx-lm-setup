# Showcase film

A 3-minute explainer of this project with motion design, rendered entirely on this Mac. [STORYBOARD.md](STORYBOARD.md) has the intent, story devices and scene list; this file covers how to build it.

```bash
.venv/bin/python examples/showcase/draft.py                # local first drafts of every scene from brief.json (Gemma via iterate), fact-checked
.venv/bin/python examples/showcase/draft.py --compare      # how much of the edited script.json came from the drafts
.venv/bin/python examples/showcase/draft.py --check-script # fact-check the edited script (local NLI per sentence, numbers by code)
.venv/bin/python examples/showcase/cards.py                # live data: the job-card sort, the PR decision, the generated contrast
python3 examples/showcase/narrate.py          # narration (clone voice "narrator"), word timestamps, cues, data.json -> data/showcase/
node examples/showcase/capture.mjs            # screenshots of the real sites (needs the gateway and pipelines/dashboard_server.py running)
cd examples/showcase && npm install && npm run render     # -> data/showcase/showcase.mp4
```

- **Narration drives timing.** `script.json` holds one narration per scene with `[[cue]]` markers. `narrate.py` sends them to the gateway's `/api/narrate` (the `narrate` MCP tool), which speaks, transcribes and resolves each cue to the start time of the word after it. The script then copies the wavs and computes waveform peaks. Scenes key animations off cue names (`useCue()("evict")`), so a re-recorded line re-times its scene. `--only stage,decide` re-narrates some scenes and keeps the rest; clips are cached by the gateway, so an unchanged scene costs nothing.
- **Real data.** Model sizes come from `gateway.toml`, the Alice phrase and its scores from `data/book_events.jsonl`, and the ROC curves and the operating point from `data/results.jsonl`. The decide panel's percentages come from the live `/jev` capture (constants in `Decide.tsx`).
- **Checking a scene.** Each scene is also its own composition: `npx remotion still scene-stage out.png --frame 600 --public-dir ../../data/showcase`, or `npm run studio` to scrub.
- **Generated assets** (audio, captures, timeline, video) live in `data/showcase/`, which git ignores.
- The closing line ("the swap did not grow") was checked by sampling `vm.swapusage` every 5 s during the render: on the latest render 5.12 GB before, 5.08 GB after, never above the start (and likewise on earlier renders). Swap was not zero, because pages swapped out earlier stay swapped until touched. Re-check it if you change the ending.
