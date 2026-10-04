"""Does the model choose better when the OPTION IS THE EMOJI ITSELF (its own learned meanings) instead of my text names?

  .venv-mlxjev/bin/python bench_glyph.py

Variants (same question, same 343 emoji): A = names ("an eggplant"), B = glyph only ("🍆"), C = glyph + name hint ("🍆: an eggplant").
Runs on the 14 benchmark phrases plus slang / double-meaning probes; prints top-3 per variant and timing.
"""
import json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "models", "jevstyle-2b-mlx"))
from emoji_vocab import EMOJI
from jev_style_decision_mlx import JevStyleDecisionMLX

Q = "Which emoji best expresses the meaning or mood of this passage?"
GLYPHS = [e for e, _ in EMOJI]
NAMES = [n for _, n in EMOJI]
VARIANTS = {
    "A names": NAMES,
    "B glyph": GLYPHS,
    "C glyph+name": {e: n for e, n in EMOJI},
}
PROBES = [
    "She sent him a peach and an eggplant and a knowing look.",
    "Netflix and chill tonight?",
    "Honestly that take is cap, no cap.",
    "The party last night was absolutely lit.",
    "He kept it a hundred and never ghosted anyone.",
    "Time to go touch grass.",
    "She is such a snake, and he was a clown for trusting her.",
    "We sat in the sun eating cake and sipping tea.",
]

m = JevStyleDecisionMLX(os.path.join(HERE, "models", "jevstyle-2b-mlx"), precision="8bit")
phrases = json.load(open(os.path.join(HERE, "data", "bench_phrases.json")))
m.decide(phrases[0], Q, options=NAMES)  # warm-up
res = {v: [] for v in VARIANTS}; times = {v: [] for v in VARIANTS}
texts = phrases + PROBES
for ph in texts:
    for v, opts in VARIANTS.items():
        t = time.time(); r = m.decide(ph, Q, options=opts); times[v].append(time.time() - t)
        sc = r["scores"]; keys = list(opts) if isinstance(opts, dict) else opts
        order = np.argsort([-sc[k] for k in keys])[:3]
        res[v].append([GLYPHS[i] for i in order])
    tag = "PROBE" if ph in PROBES else "     "
    print(f"{tag} {ph[:52]:52s} " + "  ".join(f"{v[0]}:{''.join(res[v][-1]):6s}" for v in VARIANTS), flush=True)

print("\nmean s/phrase:", {v: round(float(np.mean(t)), 2) for v, t in times.items()})
top = lambda l: set(l)
n = len(phrases)
for a, b in [("A names", "B glyph"), ("A names", "C glyph+name"), ("B glyph", "C glyph+name")]:
    ov = np.mean([len(top(res[a][i]) & top(res[b][i])) / 3 for i in range(len(texts))])
    print(f"top-3 overlap {a} vs {b}: {ov:.2f}")
json.dump({"texts": texts, "res": res, "times": times}, open(os.path.join(HERE, "data", "bench_glyph.json"), "w"), ensure_ascii=False)
