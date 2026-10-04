"""Local-context variants for the Jev-Style 2B scorer: state = [before] ⟦chunk⟧ [after], question points at the marked passage."""
import json, os, sys, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "models", "jevstyle-2b-mlx"))
from emoji_vocab import EMOJI
from jev_style_decision_mlx import JevStyleDecisionMLX
G, N = [e for e, _ in EMOJI], [n for _, n in EMOJI]
words = [w["w"] for w in json.load(open("data/book_words.json"))]
chunks = json.load(open("data/book_chunks.json"))
idx = list(range(0, 98, 7))
Q_P = "Which emoji best expresses the meaning or mood of this passage?"
Q_M = "Which emoji best expresses the meaning or mood of the passage marked ⟦ ⟧ in the text?"
m = JevStyleDecisionMLX(os.path.join(HERE, "models", "jevstyle-2b-mlx"), precision="8bit")
m.decide("warm up", Q_P, options=N)
CONFIGS = {"P": None, "B15": (15, 5), "B30": (30, 10), "B60": (60, 20)}
tops = {c: [] for c in CONFIGS}; ts = {c: [] for c in CONFIGS}
def top3(r):
    return "".join(G[i] for i in np.argsort([-r["scores"][n] for n in N])[:3])
for k in idx:
    w0, w1 = chunks[k]; chunk = " ".join(words[w0:w1]); row = []
    for c, win in CONFIGS.items():
        t = time.time()
        if win is None:
            r = m.decide(chunk, Q_P, options=N)
        else:
            b, a = win
            st = " ".join(words[max(0, w0 - b):w0]) + " ⟦" + chunk + "⟧ " + " ".join(words[w1:w1 + a])
            r = m.decide(st.strip(), Q_M, options=N)
        ts[c].append(time.time() - t); tops[c].append(top3(r)); row.append(f"{c}:{tops[c][-1]:7s}")
    print(f"{chunk[:40]:40s} " + " ".join(row), flush=True)
print("\nmean s/phrase:", {c: round(float(np.mean(v)), 2) for c, v in ts.items()})
