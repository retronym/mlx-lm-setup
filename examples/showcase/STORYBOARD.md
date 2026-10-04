# Showcase explainer: storyboard

Status: **built** (see [README.md](README.md)). The film follows this storyboard, with the deviations listed under "As built" at the end.

## Intent

A 3½-minute film that makes one argument and earns it: *a single Mac can host a private AI back end if something manages its memory, and most useful work is deciding, not generating.* It should feel like a short motion-design piece, not a slideshow: persistent objects that carry meaning from scene to scene, diagrams that show the real mechanism, and a bookended story with a payoff.

The film is also its own demo: the script, voice, captions, timing and render all come from this repo on this Mac, and the last act says so.

## Storytelling devices

1. **The memory ribbon (the recurring HUD).** A horizontal 48 GB bar along the bottom of the frame, drawn to scale, with a 28 GB budget line and a small swap gauge at its right end. Models are coloured blocks whose width is their real resident size. The viewer learns to read it in act 1; later acts tell their story through it without narration (a block fades as a model passivates, an old block slides out when a new one needs room, the swap needle stays at zero). It is the film's through-line and its final shot.
2. **Bookend: the night the Mac ran out of room.** The cold open is the real incident from the findings log (two copies of Qwen3-Coder plus OpenJev plus browsers, swap at 6 of 7 GB). The final shot is the same ribbon, calm, while this very film is rendering.
3. **One metaphor, used sparingly: the stage manager.** The gateway is a stage manager; models are performers who are called on when a cue needs them and sent to the wings when idle. It appears in one scene (act 2) to make passivation intuitive, then the film drops to the literal diagram. It is not carried as costume through the whole film.
4. **Show the mechanism, not a box labelled "AI".** The decision-model scene animates the actual computation (state computed once, options appended, `logit(yes) − logit(no)` read at each option's slot), using a real Alice phrase and its real scores.
5. **Honesty beats.** Two short moments where a local model is wrong (the invented IndexError; OpenJev's 0.72 entailment of it). They motivate the gates and the "keep judgment on the big model" split, and they make the film credible to an expert audience.
6. **The meta reveal.** In the last act the narrator's own waveform appears, the current words light up from Whisper timestamps, and the narration says it was designed from a sentence of description and cloned for consistency.

## Visual language

- Dark warm-grey ground, off-white type, **one accent: the README's warning orange `#d95926`**, used only for "this is the problem" and the active element. Model families get muted, distinct hues (LLMs, decision, NLI, speech) kept constant all film.
- Type: a grotesk for headings, a monospace for ports, tool names and numbers. Numbers are real and tabular-aligned; they count up rather than appear.
- Motion: ease-out for arrivals, ease-in-out for morphs, nothing bounces. Every move means something (arrive, transform, leave). Scenes **morph** into each other through shared objects (the ribbon, the gateway box, a model block) instead of cutting.
- Real UI is shown as **framed captures** of the actual pages (`/`, `/jev`, `/admin`, `/speech`, `book.html`, the triage dashboard), slowly pushed in, with the relevant element highlighted. Not mocked up.
- Captions burned in, two lines max, with the current word subtly emphasised.

## Scenes

Timings are targets (narration at about 150 words a minute). Narration is a first draft; `[[cue]]` marks a moment the animation must hit (see "Supporting changes").

### 0. Cold open: out of room (0:00–0:18)

- **Visual:** black. A macOS-style memory pressure line draws left to right, green, then yellow. The memory ribbon fades up beneath it. Blocks drop in to scale with labels: `Qwen3-Coder 17 GB`, then a second identical block `Qwen3-Coder (again) 17 GB` in orange, `OpenJev 9 GB`, `browsers`. The last block does not fit: it pushes through the right end of the bar and spills downward into a "swap" tray that fills to 6 of 7 GB. The pressure line goes red. Everything stutters (frame-drop effect, held for a beat).
- **Narration:** "One evening in October, this Mac ran out of room. [[dup]] Two copies of the same thirty-billion-parameter model, a fact-checking model, and a few browser tabs. [[swap]] Six gigabytes of swap, and a machine that could barely move."
- **Device:** bookend setup; introduces the ribbon by example, before it is explained.

### 1. Title (0:18–0:24)

- **Visual:** the spilled blocks lift off the ribbon and dissolve; the ribbon empties and settles. Title types onto the empty bar: **A private AI back end for one Mac.** Subtitle: *open-weight models · Apple silicon · nothing leaves the machine.*
- **Narration:** "This is the fix: a private AI back end for one Mac."

### 2. The constraint is memory (0:24–0:50)

- **Visual:** the ribbon grows to centre frame and becomes a proper diagram: 48 GB with ticks, a dashed **28 GB budget** line, the remainder labelled "macOS, the JVM, sbt, you". The catalog's models line up above it as blocks to scale (Qwen3-Coder 17, Qwen3.6 21.5, Gemma 4 16, OpenJev 9, Jev-Style 3, Kokoro 0.9, Qwen3-TTS 3.7, Whisper 2). Try to drop them all in: they overflow the budget several times over. Then a small "speed" dial beside it reads ~100 tok/s, healthy; the camera stays on the ribbon.
- **Narration:** "The binding constraint is not speed. These models generate a hundred tokens a second. It's memory. [[overflow]] Together they need several times what the budget allows. So they cannot all be resident; something has to decide who is, and when."
- **Device:** sizes to scale do the arguing.

### 3. One front door (0:50–1:10)

- **Visual:** a scatter of the old world: little doors labelled `:8080`, `:8766`, `score_worker.py`, `serve.sh`, each with its own wire to its own model copy. They slide together and collapse into one door, **`127.0.0.1:8090`**. Behind it, the gateway box; in front of it, three clients arrive and connect: Claude Code (MCP), a browser (chat, admin), and any OpenAI client (`/v1`). Brief framed captures of the chat site and admin page slide past the clients.
- **Narration:** "Before, every model was its own script, its own port, sometimes its own private copy of a model. Now there is one front door. Claude Code speaks MCP to it, the browser gets a chat site and an admin page, and anything that speaks the OpenAI API just works."

### 4. The stage manager (1:10–1:45)

- **Visual:** the gateway box opens into a stage seen from above: a lit stage area (the budget) and dim wings. Model blocks wait in the wings. A request arrives addressed to `jevstyle-2b`: its block walks on (Starting, a ~2 s spinner), lights up (Ready), serves (Busy pulse), and a **thin idle ring** starts draining around it. Three simultaneous requests arrive for a cold Qwen: one cue, one start, all three wait and are served in turn (single-flight). Then the payoff: Qwen3.6 is requested but would not fit; the block whose ring is emptiest (least recently used) walks off, and its memory visibly returns to the ribbon before Qwen3.6 walks on. The metaphor then **flattens into the real state diagram** (Stopped → Starting → Ready ⇄ Busy → Passivating → Stopped) with the same block tracing the path.
- **Narration:** "The gateway works like a stage manager. A model waits in the wings until a request needs it, [[start]] then it's called on in about two seconds. When it has been idle long enough, it leaves the stage, and its memory goes back. [[single]] Three requests for a cold model share one start. [[evict]] And when a newcomer won't fit, the one that has been idle longest makes room. Passivation means the process exits, because on Metal that's the only reliable way to give memory back."
- **Device:** the metaphor is introduced, used once, then explicitly replaced by the literal diagram, so experts are not patronised.

### 5. Decide, don't generate (1:45–2:30)

The heart of the film; longest scene.

- **Visual, part A (the contrast):** split screen. Left, "generate": a model types an answer token by token, a cursor blinking, a counter of tokens and seconds ticking up, the answer free text that must then be parsed. Right, "decide": the same question with a closed list of options; a single pulse passes through the model and **every option lights up at once** with a probability bar. Left is still typing.
- **Visual, part B (the mechanism):** zoom into the right side. A real phrase from *Alice* sits in a context window (15 words before, the phrase, 5 after: "about 25 tokens"). It is encoded once into a glowing "state" slab. Four questions slot in beneath it: emoji (346 options), vibe colour (12), mood (10), sentiment (5 ordered). Each option gets a slot; at each slot two logits (yes, no) are read and subtracted; the differences become a softmax bar chart. The top three emoji rise up and settle as ruby text above the phrase, and the phrase takes on the colour tint.
- **Visual, part C (cost):** a small counter comparison: one forward pass, about a second, deterministic; extra questions cost only their own options.
- **Narration:** "The most useful lesson here is to decide instead of generate. [[pulse]] Give a model a closed list of options, and a decision model scores every one of them in a single forward pass, with probabilities. Here's a phrase from Alice in Wonderland. [[state]] The context is read once. [[questions]] Then each question adds only its options, and each option's score is how much more the model leans yes than no. Emoji, colour, mood and sentiment, for the price of one pass. It's fast, it's deterministic, and nothing has to be parsed."

### 6. Pipelines are plain code around scorers (2:30–2:55)

- **Visual:** pull back from the annotated phrase: it is one of 748 on a slow conveyor of phrases (spaCy chunker → Jev-Style → fused with LLM log-probabilities → page). Cut-free transition into a framed capture of `book.html` scrolling, emoji above every phrase, tinted text, a hover popup opening. Then a second, shorter vignette: PR titles from scala/scala stream past six yes/no hypotheses; an ROC curve draws itself (AUROC 0.80 to 0.99), followed by a candid note: *well ranked, badly calibrated.*
- **Narration:** "So the pipelines are ordinary code with a scorer inside. Seven hundred and forty-eight phrases of Alice, each with its emoji and colour. Three hundred Scala pull requests, triaged against six questions. Good at ranking; less good at knowing where to draw the line."

### 7. Trust, but gate (2:55–3:20)

- **Visual:** the honesty beat. A code snippet (`moving_avg`) and the local LLM's confident review: "raises IndexError". The NLI model checks the claim and, with 0.72, agrees: both wrong, highlighted orange. Then the `iterate` loop as a circular diagram: a candidate goes out, passes through gates (JSON, regex, length, faithfulness), fails one, the failure is fed back, the next candidate passes and exits. To the side, a split: the hosted model (Claude) holds "design, review, judgment"; the local models hold "summaries, extraction, classification, boilerplate", with MCP tool names (`chat`, `decide`, `entail`, `iterate`) as the arrows between.
- **Narration:** "Local models are wrong in confident ways. [[wrong]] This one invented a bug, and the fact-checker agreed with it. So local work goes through gates: [[gate]] a candidate is retried until it passes checks a machine can verify. Claude keeps design and judgment, and hands the cheap, checkable work down to the Mac."

### 8. And now it can talk: the reveal (3:20–3:45)

- **Visual:** the narrator's own waveform draws across the frame **as it is being spoken**; the words being said light up above it from Whisper timestamps. A one-line voice description types out ("a calm, warm narrator…") and morphs into a waveform clip labelled `narrator.wav`, which then feeds the clone model for every scene. Small chips: Kokoro, Qwen3-TTS design, Qwen3-TTS clone, Whisper, with their sizes.
- **Narration:** "Which brings us to this voice. It was designed from a single sentence of description, then cloned so every scene sounds the same. [[words]] Whisper listened back and timed every word you've seen on screen. The script, the voice, the captions and this film were all made on this Mac."

### 9. Bookend (3:45–3:55)

- **Visual:** the memory ribbon from the cold open returns to full frame. Small blocks: the gateway, the clone TTS model (3.7 GB), Whisper (2 GB). A faint render progress bar crawls along the top: this film. The swap gauge needle sits at zero. Hold. Fade to the repo name and `localhost:8090`.
- **Narration:** "And while it rendered, the swap stayed at zero."

## How it would be built

- **Renderer: [Remotion](https://www.remotion.dev/)** (React components, frame-based, headless `npx remotion render`, mature and well tested; free for individuals and small companies, worth a licence check if this becomes company work). Every scene is a component driven by a timeline JSON; Claude can render a single scene or a single frame to check its work, which matters more than the editor. Alternatives: Motion Canvas (lovely timeline model, but headless rendering is less mature) and Manim (great for maths, heavy for UI-flavoured work).
- **Narration and timing** come from the gateway unchanged: one `speak` call per scene with the existing `narrator` clone voice, then `transcribe` for word timestamps.
- **Real captures** of the sites via the built-in browser or Playwright, saved as PNGs into the project, with element bounding boxes recorded so highlights land on the real UI.
- **Real data** read at build time from the repo (`gateway.toml` sizes, `data/book_events.jsonl` for the Alice phrase, `data/results.jsonl` for the ROC curve) so the numbers on screen are true.

## Supporting changes

- **Cue markers in narration.** `[[cue]]` tokens are stripped before synthesis and resolved to times by aligning the word after the marker against Whisper's word timestamps. Animations key off named cues instead of hand-typed seconds, so re-recording a line re-times the scene automatically. This belongs in the explainer pipeline, not the gateway, and the existing `examples/explainer/build.py` could use it too.
- **A small timing export**: one JSON per film with each scene's audio path, duration, words and resolved cues, which the Remotion project imports.

## Open questions

1. **Remotion** (recommended) or Motion Canvas?
2. **Voice:** reuse the existing `narrator` clone, or design a new one for this film?
3. **Length:** 3½ minutes as above, or a tighter 2-minute cut (drop scenes 3 and 6)?
4. **Music:** none (recommended; keeps it local and licence-free) or a quiet bed?
5. **The stage-manager metaphor:** keep it as a one-scene device, or go straight to the state diagram?

## As built

- **Defaults taken:** Remotion, the existing `narrator` clone voice, the full length (3:03), no music, and the stage-manager metaphor kept as a one-scene device.
- **Cold open:** "macOS + apps" (8 GB) is drawn as a block, so the overflow into swap comes to exactly the 6 GB from the findings log.
- **Stage scene:** the eviction uses OpenJev 4B as the newcomer (3 + 17.5 + 9.5 > 28 GB, and evicting the least recently used model, Jev-Style, makes room). Qwen3.6 would have needed two evictions.
- **Pipelines:** the narration no longer says "748 phrases", because only 315 had been scored when the film was made.
- **Ending:** the line is "the swap did not grow" rather than "stayed at zero", because swap was already 7.6 GB from earlier work. The ribbon shows every model passivated, which is true at render time, and the swap was measured during the render.
- **Not done:** a separate timing export (the timeline JSON plays that role), and narration changes reviewed by ear (Whisper round-trips every scene).
