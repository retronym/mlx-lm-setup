"""Jev-Style decision model (MLX) as a small JSON server.  Runs in .venv-mlxjev (pinned mlx / mlx-lm).

  POST /decide     {"state": str, "question": str | {"t","ins","crit"}, "options"?: list|dict, "qtype"?: str, "temperature"?: float}
  POST /score_many {"state": str, "questions": [{"t","ins","crit"} ...], "temperature"?: float}   # state computed once
  Both return the runtime's result dicts: {"answer", "probabilities", "scores", ...}
"""
import argparse, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--model-dir", required=True)
ap.add_argument("--precision", default="8bit")
ap.add_argument("--port", type=int, required=True)
a = ap.parse_args()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, a.model_dir)
from jev_style_decision_mlx import JevStyleDecisionMLX      # noqa: E402
from _http import serve                                      # noqa: E402

t0 = time.time()
m = JevStyleDecisionMLX(a.model_dir, precision=a.precision)
print(f"model loaded in {time.time() - t0:.1f}s", flush=True)


def decide(r):
    return m.decide(r["state"], r["question"], options=r.get("options"), qtype=r.get("qtype"), temperature=r.get("temperature"))


def score_many(r):
    return m.score_many(r["state"], r["questions"], temperature=r.get("temperature"))


serve(a.port, {"model": "Jev-Style-2B-Decision-v3", "precision": a.precision, "load_s": round(time.time() - t0, 1)},
      {"/decide": decide, "/score_many": score_many})
