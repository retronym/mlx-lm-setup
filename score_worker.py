"""Score every book phrase with ONE model and append raw per-emoji scores to data/scores_<mode>.jsonl (resumable).
  /opt/homebrew/opt/mlx-lm/libexec/bin/python score_worker.py lm    # Qwen3-Coder log P(emoji | few-shot)
  .venv-mlxjev/bin/python             score_worker.py jev   # Jev-Style 2B (names as options)
"""
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from emoji_vocab import EMOJI
mode = sys.argv[1]; fresh = "--fresh" in sys.argv
D = os.path.join(HERE, "data"); OUT = os.path.join(D, f"scores_{mode}.jsonl")
words = [w["w"] for w in json.load(open(os.path.join(D, "book_words.json")))]
chunks = json.load(open(os.path.join(D, "book_chunks.json")))
if fresh and os.path.exists(OUT): os.remove(OUT)
done = sum(1 for _ in open(OUT)) if os.path.exists(OUT) else 0
G, N = [e for e, _ in EMOJI], [n for _, n in EMOJI]
if mode == "lm":
    from lm_emoji import LMEmoji
    lm = LMEmoji("mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit", G)
    score = lambda ph: lm.logprobs(ph).tolist()
else:
    sys.path.insert(0, os.path.join(HERE, "models", "jevstyle-2b-mlx"))
    from jev_style_decision_mlx import JevStyleDecisionMLX
    m = JevStyleDecisionMLX(os.path.join(HERE, "models", "jevstyle-2b-mlx"), precision="8bit")
    from jev_attrs import ATTRS, emoji_question, make_state, attr_result
    EMOJI_Q = emoji_question(N)
    names = list(ATTRS)
    def score(ph, w0=None, w1=None):
        rs = m.score_many(make_state(words, w0, w1), [EMOJI_Q] + [ATTRS[a] for a in names])   # state computed once, 4 questions
        return [rs[0]["scores"][n] for n in N], {a: attr_result(a, r) for a, r in zip(names, rs[1:])}
with open(OUT, "a") as f:
    for i in range(done, len(chunks)):
        w0, w1 = chunks[i]; t = time.time()
        ph = " ".join(words[w0:w1])
        if mode == "lm":
            rec = {"i": i, "s": [round(x, 3) for x in score(ph)]}
        else:
            sc, attrs = score(ph, w0, w1); rec = {"i": i, "s": [round(x, 3) for x in sc], "attrs": attrs}
        rec["t"] = round(time.time() - t, 2)
        f.write(json.dumps(rec, ensure_ascii=False) + "\n"); f.flush()
print(mode, "finished")
