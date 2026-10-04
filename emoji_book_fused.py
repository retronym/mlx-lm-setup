"""Combine the two workers' scores (z-score fusion, w_lm=0.6, LM logprobs floored at max-12) into book_events.jsonl.
  .venv-jev/bin/python emoji_book_fused.py [--fresh]
"""
import json, os, sys, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from emoji_vocab import EMOJI
D = os.path.join(HERE, "data")
W_LM, FLOOR, TOP_K, MIN_Z = 0.6, 12.0, 3, 1.5
words = [w["w"] for w in json.load(open(f"{D}/book_words.json"))]
chunks = json.load(open(f"{D}/book_chunks.json"))
EV, ST = f"{D}/book_events.jsonl", f"{D}/book_status.json"
if "--fresh" in sys.argv and os.path.exists(EV): os.remove(EV)
k = sum(1 for _ in open(EV)) if os.path.exists(EV) else 0
status = dict(state="running", cursor=chunks[k][0] if k < len(chunks) else len(words), phrases=k, total_words=len(words), vocab=len(EMOJI),
              started=time.time(), stage="scoring", model="fused: Jev-Style 2B + Qwen3-Coder-30B", device="mlx")
def save():
    json.dump(status, open(ST + ".tmp", "w"), ensure_ascii=False); os.replace(ST + ".tmp", ST)
def z(x, floor=None):
    x = np.array(x, float)
    if floor is not None: x = np.maximum(x, x.max() - floor)
    return (x - x.mean()) / (x.std() + 1e-9)
def read(mode):
    p = f"{D}/scores_{mode}.jsonl"
    return [json.loads(l) for l in open(p) if l.strip()] if os.path.exists(p) else []
save()
with open(EV, "a") as out:
    while k < len(chunks):
        lm, jv = read("lm"), read("jev")
        while k < len(jv):                       # Jev is the backbone; fuse with the LLM where its scores exist
            w0, w1 = chunks[k]
            b = np.array(jv[k]["s"]); has_lm = k < len(lm)
            a = np.array(lm[k]["s"]) if has_lm else np.full(len(b), -30.0)
            s = (W_LM * z(a, FLOOR) + (1 - W_LM) * z(b)) if has_lm else z(b)
            order = np.argsort(-s); p_lm = np.exp(a)
            mk = lambda i: dict(e=EMOJI[i][0], name=EMOJI[i][1], p=round(float(p_lm[i]), 3), z=round(float(s[i]), 2))
            top = [mk(int(i)) for i in order[:TOP_K] if s[i] >= MIN_Z]
            ev = dict(i=k, w0=w0, w1=w1, text=" ".join(words[w0:w1]), top=top, alts=[mk(int(i)) for i in order[:8]],
                      seg=dict(cands=[], p=[], chosen=w1 - w0), attrs=jv[k].get("attrs", {}), secs_seg=0.0, secs_emoji=round((lm[k]["t"] if has_lm else 0) + jv[k]["t"], 2), fused=has_lm)
            out.write(json.dumps(ev, ensure_ascii=False) + "\n"); out.flush()
            k += 1; status.update(cursor=w1, phrases=k, stage="scoring", last_secs=ev["secs_emoji"]); save()
            print(f"[{k:4d}] {''.join(t['e'] for t in top) or '·':<6} {ev['text']}", flush=True)
        time.sleep(0.4)
status.update(state="finished", stage="done"); save()
