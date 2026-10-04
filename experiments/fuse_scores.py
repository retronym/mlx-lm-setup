"""Compute raw per-emoji scores for data/fuse_texts.json with ONE of the models; fusion is done offline in fuse_eval.py.
  /opt/homebrew/opt/mlx-lm/libexec/bin/python fuse_scores.py lm      -> data/fuse_lm.json    (Qwen3-Coder log P(emoji | few-shot))
  .venv-mlxjev/bin/python             fuse_scores.py jev     -> data/fuse_jev.json   (Jev-Style 2B, names as options)
"""
import json, os, sys, time
import numpy as np
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repo root: data/ and models/ live there
sys.path[:0] = [os.path.join(HERE, "experiments"), os.path.join(HERE, "pipelines", "emoji_book")]; sys.path.insert(0, HERE)
from emoji_vocab import EMOJI
EXTRA = [("🍆", "an eggplant"), ("💦", "splashing water"), ("🥵", "feeling hot")]
V = EMOJI + [x for x in EXTRA if x[0] not in {e for e, _ in EMOJI}]
G, N = [e for e, _ in V], [n for _, n in V]
texts = json.load(open(os.path.join(HERE, "data", "fuse_texts.json")))
mode = sys.argv[1]
out, ts = [], []
if mode == "lm":
    from lm_emoji import LMEmoji
    lm = LMEmoji("mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit", G)
    lm.logprobs("warm up")
    for t in texts:
        s = time.time(); out.append(lm.logprobs(t["text"]).tolist()); ts.append(time.time() - s)
else:
    sys.path.insert(0, os.path.join(HERE, "models", "jevstyle-2b-mlx"))
    from jev_style_decision_mlx import JevStyleDecisionMLX
    m = JevStyleDecisionMLX(os.path.join(HERE, "models", "jevstyle-2b-mlx"), precision="8bit")
    Q = "Which emoji best expresses the meaning or mood of this passage?"
    m.decide(texts[0]["text"], Q, options=N)
    for t in texts:
        s = time.time(); r = m.decide(t["text"], Q, options=N); ts.append(time.time() - s)
        out.append([r["scores"][n] for n in N])
json.dump(dict(glyphs=G, scores=out, secs=ts), open(os.path.join(HERE, "data", f"fuse_{mode}.json"), "w"), ensure_ascii=False)
print(mode, "done; mean", round(float(np.mean(ts)), 2), "s/text")
