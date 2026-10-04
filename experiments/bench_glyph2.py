"""Slang / double-meaning probes: does the model use its own knowledge of an emoji's slang meaning?
A = literal name, B = glyph only, C = glyph + literal name. Vocabulary = ours + 🍆 💦 🥵 (extras for this test)."""
import os, sys, time
import numpy as np
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repo root: data/ and models/ live there
sys.path[:0] = [os.path.join(HERE, "experiments"), os.path.join(HERE, "pipelines", "emoji_book")]
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "models", "jevstyle-2b-mlx"))
from emoji_vocab import EMOJI
from jev_style_decision_mlx import JevStyleDecisionMLX
EXTRA = [("🍆", "an eggplant"), ("💦", "splashing water"), ("🥵", "feeling hot")]
V = EMOJI + [x for x in EXTRA if x[0] not in {e for e, _ in EMOJI}]
G = [e for e, _ in V]; N = [n for _, n in V]
Q = "Which emoji best expresses the meaning or mood of this passage?"
VARS = {"A name": N, "B glyph": G, "C both": {e: n for e, n in V}}
PROBES = [  # (text, emoji whose SLANG meaning applies)
    ("That was a very flirty, suggestive joke with an innuendo.", "🍆"),
    ("Her rear view in those jeans was a real showstopper.", "🍑"),
    ("I am literally dying, that was so hilarious.", "💀"),
    ("He is the greatest of all time, the GOAT.", "🐐"),
    ("That outfit is fire.", "🔥"),
    ("Don't lie to me, that is total cap.", "🧢"),
    ("She was sweating bullets, nervous and soaked.", "💦"),
    ("He kept it real, a hundred percent honest.", "💯"),
    ("He is such a clown for believing her.", "🤡"),
    ("Everyone was ghosting him after that.", "👻"),
]
m = JevStyleDecisionMLX(os.path.join(HERE, "models", "jevstyle-2b-mlx"), precision="8bit")
m.decide(PROBES[0][0], Q, options=N)
hits = {v: [0, 0] for v in VARS}
for text, want in PROBES:
    row = []
    for v, o in VARS.items():
        r = m.decide(text, Q, options=o); keys = list(o) if isinstance(o, dict) else o
        order = np.argsort([-r["scores"][k] for k in keys]); top3 = [G[i] for i in order[:3]]
        rank = [G[i] for i in order].index(want) + 1
        hits[v][0] += rank <= 3; hits[v][1] += rank == 1
        row.append(f"{v[0]}:{''.join(top3)} (rank of {want} = {rank})")
    print(f"{text[:50]:50s} " + "   ".join(row))
print("\nslang emoji in top-3 / top-1 (of %d):" % len(PROBES), {v: tuple(h) for v, h in hits.items()})

# ---- fusion: z-score each rendering's 346 scores within the phrase, then sum -----------------------------------------
z = lambda s: (np.array(s) - np.mean(s)) / (np.std(s) + 1e-6)
print("\nfusion (z-scored A name + B glyph):")
fh = [0, 0]; fh3 = [0, 0]; t_total = 0.0
for text, want in PROBES:
    t = time.time()
    a = m.decide(text, Q, options=N)["scores"]; b = m.decide(text, Q, options=G)["scores"]
    t_total += time.time() - t
    sa = z([a[n] for n in N]); sb = z([b[g] for g in G])
    for label, s in (("A+B", sa + sb), ("A+B+C?", None)):
        if s is None: continue
        order = np.argsort(-s); rank = [G[i] for i in order].index(want) + 1
        print(f"  {text[:46]:46s} {''.join(G[i] for i in order[:3])}  rank of {want} = {rank}")
        fh[0] += rank <= 3; fh[1] += rank == 1
print("fusion A+B top-3 / top-1 hits:", tuple(fh), f"| {t_total/len(PROBES):.2f}s per phrase (2 passes)")
