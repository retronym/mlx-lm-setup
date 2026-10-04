"""Live emoji annotation with Jev-Style-2B-Decision-v3 on MLX: one forward pass scores ALL emoji per phrase.

Phrase boundaries come from data/book_chunks.json (deterministic spaCy chunker, see chunk_book.py).
Per phrase: score every emoji as an option of one "choice" question, calibrate with a running per-emoji z-score,
abstain below MIN_P, emit an event for book.html, advance.

Usage: .venv-mlxjev/bin/python emoji_book_mlx.py [--fresh] [--max-phrases N]
"""
import argparse, json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repo root: data/ and models/ live there
sys.path[:0] = [os.path.join(HERE, "experiments"), os.path.join(HERE, "pipelines", "emoji_book")]
DATA = os.path.join(HERE, "data")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "models", "jevstyle-2b-mlx"))
from emoji_vocab import EMOJI

WORDS, CHUNKS, EVENTS, STATUS = (os.path.join(DATA, f) for f in
                                 ("book_words.json", "book_chunks.json", "book_events.jsonl", "book_status.json"))
QUESTION = "Which emoji best expresses the meaning or mood of this passage?"
NAMES = [n for _, n in EMOJI]
Z_WARMUP, MIN_P, TOP_K = 6, 0.04, 3


class Welford:
    def __init__(self, n):
        self.n, self.mean, self.m2 = 0, np.zeros(n), np.zeros(n)

    def update(self, x):
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    def std(self):
        return np.sqrt(self.m2 / max(1, self.n - 1)) + 1e-3


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--max-phrases", type=int, default=0)
    a = ap.parse_args()

    words = [w["w"] for w in json.load(open(WORDS))]
    chunks = json.load(open(CHUNKS))
    if a.fresh and os.path.exists(EVENTS):
        os.remove(EVENTS)
    status = dict(state="loading", cursor=0, phrases=0, total_words=len(words), vocab=len(EMOJI), started=time.time(),
                  stage="loading model", model="jevstyle-2b-mlx-8bit", device="mlx")
    write_json_atomic(STATUS, status)

    from jev_style_decision_mlx import JevStyleDecisionMLX
    m = JevStyleDecisionMLX(os.path.join(HERE, "models", "jevstyle-2b-mlx"), precision="8bit")
    status.update(state="running", stage="idle"); write_json_atomic(STATUS, status)

    stats = Welford(len(EMOJI))
    with open(EVENTS, "a") as out:
        for k, (w0, w1) in enumerate(chunks):
            if a.max_phrases and k >= a.max_phrases:
                break
            phrase = " ".join(words[w0:w1])
            status.update(cursor=w0, phrases=k, stage=f"scoring {len(EMOJI)} emoji", phrase=phrase)
            write_json_atomic(STATUS, status)

            t0 = time.time()
            r = m.decide(phrase, QUESTION, options=NAMES)
            dt = time.time() - t0
            sc = np.array([r["scores"][n] for n in NAMES])           # logit(yes) - logit(no) per emoji
            p = np.array([r["probabilities"][n] for n in NAMES])     # softmax over all emoji

            if stats.n >= Z_WARMUP:
                z = (sc - stats.mean) / stats.std(); key = z
            else:
                z = np.zeros(len(EMOJI)); key = sc
            stats.update(sc)
            order = [int(i) for i in np.argsort(-key) if p[i] >= MIN_P][:TOP_K]
            mk = lambda i: dict(e=EMOJI[i][0], name=EMOJI[i][1], p=round(float(p[i]), 3), z=round(float(z[i]), 2))
            ev = dict(i=k, w0=w0, w1=w1, text=phrase, top=[mk(i) for i in order],
                      alts=[mk(int(i)) for i in np.argsort(-key)[:8]],
                      seg=dict(cands=[], p=[], chosen=w1 - w0), secs_seg=0.0, secs_emoji=round(dt, 2))
            out.write(json.dumps(ev, ensure_ascii=False) + "\n"); out.flush()
            status.update(cursor=w1, phrases=k + 1, stage="idle", last_secs=round(dt, 2)); write_json_atomic(STATUS, status)
            print(f"[{k+1:4d}] w{w0:>5}+{w1-w0:<2} {dt:4.2f}s {''.join(EMOJI[i][0] for i in order) or '·':<6} {phrase}", flush=True)

    status.update(state="finished", stage="done"); write_json_atomic(STATUS, status)


if __name__ == "__main__":
    main()
