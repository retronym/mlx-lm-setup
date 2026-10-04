"""Score claims against a source text with OpenJev 4B v5 (NLI cross-encoder).

Usage (library):  from jev_check import Jev; Jev().check(premise, [claims]) -> [(label, probs)]
Usage (CLI demo): .venv-jev/bin/python jev_check.py
"""
import os, sys, time
import numpy as np
import torch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repo root: data/ and models/ live there
sys.path[:0] = [os.path.join(HERE, "experiments"), os.path.join(HERE, "pipelines", "emoji_book")]
MODEL_DIR = os.path.join(HERE, "models", "openjev")
SUBFOLDER = os.environ.get("JEV_SUBFOLDER", "qwen3.5-4b-nli-v5")
LABELS = ["contradiction", "entailment", "neutral"]  # head order per model card

sys.path.insert(0, MODEL_DIR)
from modeling_openjev import OpenJevCrossEncoder  # noqa: E402


class Jev:
    def __init__(self, device=None):
        device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
        self.enc = OpenJevCrossEncoder(MODEL_DIR, subfolder=SUBFOLDER, device=device)

    def check(self, premise, claims):
        probs = self.enc.predict_hypotheses(premise, claims)
        return [(LABELS[int(np.argmax(p))], p) for p in probs]


if __name__ == "__main__":
    premise = '''
def moving_avg(xs, n):
    out = []
    for i in range(len(xs)):
        window = xs[i:i+n]
        out.append(sum(window) / n)
    return out

def first_dup(xs):
    seen = set()
    for x in xs:
        if x in seen: return x
        seen.add(x)
    return -1
'''
    claims = [
        "moving_avg raises an IndexError when n is larger than the length of xs.",                       # Qwen's hallucination
        "first_dup returns None when the list has no duplicate.",                                         # Qwen's other claim
        "For the last n-1 positions, moving_avg divides a partial window sum by n, giving wrong averages.",  # real bug
        "first_dup returns -1 when the list has no duplicate.",                                           # true
        "moving_avg returns a list with the same length as its input.",                                   # true
    ]
    t = time.time(); j = Jev(); print(f"load {time.time()-t:.1f}s")
    t = time.time(); res = j.check(premise, claims); print(f"score {time.time()-t:.2f}s\n")
    for c, (lab, p) in zip(claims, res):
        print(f"{lab:13s} c={p[0]:.2f} e={p[1]:.2f} n={p[2]:.2f}  | {c}")
