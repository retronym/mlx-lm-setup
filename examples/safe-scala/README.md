# Safe Scala film

A 2-minute explainer of the safe-Scala approach to constraining agents (Scala 3 capture checking + safe mode, TACIT), rendered on this Mac. [STORYBOARD.md](STORYBOARD.md) has the argument, story devices and scene list; this file covers how to build it.

```bash
python3 examples/safe-scala/facts.py        # compile the snippets with today's Scala 3 nightly, check the paper's numbers against arXiv -> data/safe-scala/facts.json
python3 examples/safe-scala/narrate.py      # narration (saved voice "odersky-inspired"), word timestamps, cues -> data/safe-scala/
node examples/safe-scala/capture.mjs        # screenshots of the TACIT README, the arXiv page and the Scala safe-mode docs
cd examples/safe-scala && npm install && npm run render      # -> data/safe-scala/safe-scala.mp4
node stills.mjs [scene,...]                 # a still per scene at each cue (and 2 s after), for layout checks
```

- **Every compiler message on screen is real.** `snippets/Api.scala` declares stubs with the signatures of TACIT's capability API (`library/Interface.scala`), not compiled in safe mode and marked `@assumeSafe`, like TACIT's library. Each `snippets/<name>/Agent.scala` is agent code under `import language.experimental.safe`. `facts.py` compiles each one with `scala-cli` and `scala 3.nightly`, fails if a snippet doesn't compile or fail with the expected message, and keeps the output, version and wall time for the film.
- **Every number is checked.** `paper.json` transcribes Tables 1 and 2 of the paper. `facts.py` fetches the arXiv HTML and fails unless each table row appears in it verbatim.
- **Narration drives timing** exactly as in [../showcase](../showcase): `script.json` holds the narration with `[[cue]]` markers, and scenes key animations off `useCue()("leak")` or `useWord()("boot")`.
- Generated assets (facts, audio, captures, stills, video) live in `data/safe-scala/`, which git ignores.
