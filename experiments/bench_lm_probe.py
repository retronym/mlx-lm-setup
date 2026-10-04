import json, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipelines", "emoji_book"))
from emoji_vocab import EMOJI
from lm_emoji import LMEmoji
model = sys.argv[1] if len(sys.argv) > 1 else "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
EXTRA = ["🍆", "💦", "🥵"]
G = [e for e, _ in EMOJI] + [g for g in EXTRA if g not in {e for e, _ in EMOJI}]
lm = LMEmoji(model, G)
phrases = json.load(open("data/bench_phrases.json"))
probes = [("That was a very flirty, suggestive joke with an innuendo.", "🍆"), ("Her rear view in those jeans was a real showstopper.", "🍑"),
    ("I am literally dying, that was so hilarious.", "💀"), ("He is the greatest of all time, the GOAT.", "🐐"), ("That outfit is fire.", "🔥"),
    ("Don't lie to me, that is total cap.", "🧢"), ("She was sweating bullets, nervous and soaked.", "💦"),
    ("He kept it real, a hundred percent honest.", "💯"), ("He is such a clown for believing her.", "🤡"), ("Everyone was ghosting him after that.", "👻")]
prior = lm.logprobs("something")                       # crude unconditional prior for a PMI variant
ts = []; hits = {"raw": [0, 0], "pmi": [0, 0]}
def show(p, want=None):
    t = time.time(); lp = lm.logprobs(p); ts.append(time.time() - t)
    raw = lp; pmi = lp - 0.7 * prior
    out = []
    for name, s in (("raw", raw), ("pmi", pmi)):
        order = np.argsort(-s); top = "".join(G[i] for i in order[:3])
        r = [G[i] for i in order].index(want) + 1 if want else None
        if want: hits[name][0] += r <= 3; hits[name][1] += r == 1
        out.append(f"{name}:{top}" + (f"(#{r})" if want else "") + (f" p={np.exp(raw[order[0]]):.2f}" if name == "raw" else ""))
    print(f"{(p[:46]):46s} " + "  ".join(out), flush=True)
print("--- book phrases"); [show(p) for p in phrases]
print("--- slang probes"); [show(p, w) for p, w in probes]
print(f"\nmean {np.mean(ts):.2f}s/phrase | slang hits top3/top1:", {k: tuple(v) for k, v in hits.items()}, "of", len(probes))
