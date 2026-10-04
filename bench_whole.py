"""Whole-chapter context for the Jev-Style 2B scorer.
  P  = per-phrase (state = the phrase only)                      [current setup]
  W1 = state = whole text (cached by the runtime), question quotes the chunk
  W2 = state = whole text with the chunk marked ⟦...⟧ (a pointer), no state caching
"""
import json, os, sys, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "models", "jevstyle-2b-mlx"))
from emoji_vocab import EMOJI
from jev_style_decision_mlx import JevStyleDecisionMLX

G, N = [e for e, _ in EMOJI], [n for _, n in EMOJI]
words = [w["w"] for w in json.load(open("data/book_words.json"))]
chunks = json.load(open("data/book_chunks.json"))
idx = list(range(0, 98, 7))                                   # the 14 benchmark phrases (every 7th of the first 98 chunks)
Q_P = "Which emoji best expresses the meaning or mood of this passage?"
Q_W1 = "Which emoji best expresses the meaning or mood of this passage from the text above: “{chunk}”"
Q_W2 = "Which emoji best expresses the meaning or mood of the passage marked ⟦ ⟧ in the text?"
m = JevStyleDecisionMLX(os.path.join(HERE, "models", "jevstyle-2b-mlx"), precision="8bit")
whole = " ".join(words)
print("whole-text words:", len(words), flush=True)
m.decide(" ".join(words[:10]), Q_P, options=N)                # warm-up

def top3(r):
    order = np.argsort([-r["scores"][n] for n in N])[:3]
    return "".join(G[i] for i in order), [G[i] for i in np.argsort([-r["scores"][n] for n in N])]

rows = []
for k in idx:
    w0, w1 = chunks[k]; chunk = " ".join(words[w0:w1])
    t = time.time(); rp = m.decide(chunk, Q_P, options=N); tp = time.time() - t
    t = time.time(); r1 = m.decide(whole, Q_W1.format(chunk=chunk), options=N); t1 = time.time() - t
    marked = " ".join(words[:w0]) + " ⟦" + chunk + "⟧ " + " ".join(words[w1:])
    t = time.time(); r2 = m.decide(marked, Q_W2, options=N); t2 = time.time() - t
    a, b, c = top3(rp), top3(r1), top3(r2)
    rows.append((chunk, a[0], b[0], c[0], tp, t1, t2, r1["input_tokens"]))
    print(f"{chunk[:42]:42s} P:{a[0]:6s} W1:{b[0]:6s} W2:{c[0]:6s}  {tp:.1f}s {t1:.1f}s {t2:.1f}s  ({r1['input_tokens']} tok)", flush=True)
ov = lambda x, y: np.mean([len(set(r[x]) & set(r[y])) / 3 for r in rows])
print(f"\nmean s/phrase: P {np.mean([r[4] for r in rows]):.2f}  W1 {np.mean([r[5] for r in rows]):.2f}  W2 {np.mean([r[6] for r in rows]):.2f}")
print(f"top-3 overlap  P~W1 {ov(1,2):.2f}   P~W2 {ov(1,3):.2f}   W1~W2 {ov(2,3):.2f}")
