"""Re-answer ONLY the colour question for phrases already in scores_jev.jsonl (cheap: tiny question on a ~25-token state).
  python3 pipelines/emoji_book/recolor.py         # via the gateway's decision model
"""
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
import gateway_client as gw
from jev_attrs import COLOR_Q, make_state, attr_result
D = os.path.join(ROOT, "data"); P = os.path.join(D, "scores_jev.jsonl")
words = [w["w"] for w in json.load(open(os.path.join(D, "book_words.json")))]
chunks = json.load(open(os.path.join(D, "book_chunks.json")))
rows = [json.loads(l) for l in open(P) if l.strip()]
t0 = time.time()
for r in rows:
    w0, w1 = chunks[r["i"]]
    r["attrs"]["color"] = attr_result("color", gw.decide_many(make_state(words, w0, w1), [COLOR_Q])[0])
with open(P + ".tmp", "w") as f:
    for r in rows: f.write(json.dumps(r, ensure_ascii=False) + "\n")
os.replace(P + ".tmp", P)
print(f"recoloured {len(rows)} phrases in {time.time()-t0:.0f}s")
