"""Score every book phrase with ONE model via the gateway and append raw per-emoji scores to data/scores_<mode>.jsonl (resumable).
  python3 pipelines/emoji_book/score_worker.py jev    # Jev-Style 2B decision model (names as options), plus colour, mood and sentiment
  python3 pipelines/emoji_book/score_worker.py lm     # Qwen3-Coder log P(emoji | few-shot), via /api/score
The gateway (.venv/bin/python -m gateway) starts the model on first use and keeps it inside the memory budget.
"""
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
from emoji_vocab import EMOJI
import gateway_client as gw
mode = sys.argv[1]; fresh = "--fresh" in sys.argv
D = os.path.join(ROOT, "data"); OUT = os.path.join(D, f"scores_{mode}.jsonl")
words = [w["w"] for w in json.load(open(os.path.join(D, "book_words.json")))]
chunks = json.load(open(os.path.join(D, "book_chunks.json")))
if fresh and os.path.exists(OUT): os.remove(OUT)
done = sum(1 for _ in open(OUT)) if os.path.exists(OUT) else 0
G, N = [e for e, _ in EMOJI], [n for _, n in EMOJI]
if mode == "lm":
    from lm_prompt import prompt
    CANDS = [" " + g for g in G]
    score = lambda ph: gw.score(prompt(ph), CANDS)
else:
    from jev_attrs import ATTRS, emoji_question, make_state, attr_result
    EMOJI_Q = emoji_question(N)
    names = list(ATTRS)
    def score(ph, w0=None, w1=None):
        rs = gw.decide_many(make_state(words, w0, w1), [EMOJI_Q] + [ATTRS[a] for a in names])   # state computed once, 4 questions
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
