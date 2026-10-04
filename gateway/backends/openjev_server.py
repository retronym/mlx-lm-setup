"""OpenJev NLI cross-encoder (PyTorch / MPS) as a small JSON server.  Runs in .venv-jev.

  POST /entail {"premise": str, "hypotheses": [str, ...]}  ->  {"labels": [...], "probs": [[contradiction, entailment, neutral], ...]}
"""
import argparse, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--root", required=True)             # directory containing modeling_openjev.py and the checkpoint subfolders
ap.add_argument("--subfolder", required=True)
ap.add_argument("--port", type=int, required=True)
a = ap.parse_args()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, a.root)
import torch                                          # noqa: E402
from modeling_openjev import OpenJevCrossEncoder      # noqa: E402
from _http import serve                               # noqa: E402

t0 = time.time()
device = "mps" if torch.backends.mps.is_available() else "cpu"
enc = OpenJevCrossEncoder(a.root, subfolder=a.subfolder, device=device)
enc.predict_hypotheses("warm up", ["a", "b", "c"])    # compile / allocate before declaring ready
print(f"model loaded in {time.time() - t0:.1f}s on {device}", flush=True)
LABELS = ["contradiction", "entailment", "neutral"]


def entail(r):
    hyps = r["hypotheses"]
    if not isinstance(hyps, list) or not hyps or not all(isinstance(h, str) for h in hyps):
        raise ValueError("hypotheses must be a non-empty list of strings")
    probs = []
    for i in range(0, len(hyps), 64):
        probs += enc.predict_hypotheses(r["premise"], hyps[i:i + 64]).tolist()
    return {"labels": LABELS, "probs": probs}


serve(a.port, {"model": "openjev", "subfolder": a.subfolder, "device": device, "load_s": round(time.time() - t0, 1)},
      {"/entail": entail})
