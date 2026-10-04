"""Emoji-annotate a book, one natural phrase at a time, with OpenJev as the only model call.

Deterministic loop (no text generation anywhere):
  1. window   = next 32 words from the cursor
  2. segment  = score "a natural phrase ends after <last 3 words>" for each candidate length with Jev;
                a plain rule picks the boundary (never crosses a sentence end)
  3. emoji    = score "This passage is about <name>." for every emoji in the vocabulary against the phrase
  4. rank     = running per-emoji z-score calibration, abstain if nothing is confident
  5. emit event -> data/book_events.jsonl, advance cursor by the phrase length, repeat

Usage: .venv-jev/bin/python emoji_book.py [--fresh] [--max-phrases N] [--vocab N]
Env:   JEV_SUBFOLDER=qwen3.5-2b-nli-v5 to use another checkpoint (see jev_check.py)
"""
import argparse, json, math, os, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
WORDS, EVENTS, STATUS, LOGITS = (os.path.join(DATA, f) for f in
                                 ("book_words.json", "book_events.jsonl", "book_status.json", "book_logits.jsonl"))

WINDOW = 32
MIN_LEN, MAX_LEN = 3, 14          # candidate phrase lengths in words
SEG_TEMPLATE = "This is a complete phrase that makes sense on its own."   # scored on the candidate prefix alone
# an unpunctuated phrase may not END on one of these (deterministic guard; found necessary in testing)
STOP_END = set("""a an the of to and or but in on at by for with from as that which her his its their my your our
she he it they i you we was were is be been had has have not so if than then into very too how what when while
who whom whose this these those there here about over under up down out off all some any no nor""".split())
EMOJI_TEMPLATE = "This passage is about {name}."
CHUNK = 64                        # hypotheses per forward pass
Z_WARMUP = 6                      # phrases before z-scores are trusted
MIN_P = 0.30                      # abstain unless a raw P(entail) clears this
TOP_K = 3


def is_sentence_end(w):
    return w.rstrip("”’\"')]").endswith((".", "?", "!"))


def logit(p, eps=1e-4):
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


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
    ap.add_argument("--vocab", type=int, default=0, help="use only the first N emoji (0 = all)")
    a = ap.parse_args()

    words = [w["w"] for w in json.load(open(WORDS))]
    from emoji_vocab import EMOJI
    if a.vocab:
        EMOJI = EMOJI[:a.vocab]
    emoji_hyps = [EMOJI_TEMPLATE.format(name=n) for _, n in EMOJI]

    cursor, k = 0, 0
    if a.fresh:
        for f in (EVENTS, LOGITS):
            if os.path.exists(f):
                os.remove(f)
    stats = Welford(len(EMOJI))
    if os.path.exists(EVENTS) and os.path.exists(LOGITS):  # resume: rebuild cursor + calibration
        for line in open(EVENTS):
            if line.strip():
                ev = json.loads(line)
                cursor, k = ev["w1"], ev["i"] + 1
        for line in open(LOGITS):
            if line.strip():
                stats.update(np.array(json.loads(line), dtype=float))

    status = dict(state="loading", cursor=cursor, phrases=k, total_words=len(words), vocab=len(EMOJI),
                  window=WINDOW, started=time.time(), stage="loading model")
    write_json_atomic(STATUS, status)

    from jev_check import Jev, SUBFOLDER
    import torch
    jev = Jev()
    status.update(model=SUBFOLDER, device=str(jev.enc.device), state="running")

    def score(premise, hyps):
        out = []
        for i in range(0, len(hyps), CHUNK):
            out.append(jev.enc.predict_hypotheses(premise, hyps[i:i + CHUNK]))
        return np.concatenate(out)                  # [n, 3] = contradiction, entailment, neutral

    n_phrases = 0
    with open(EVENTS, "a") as out:
        while cursor < len(words):
            if a.max_phrases and n_phrases >= a.max_phrases:
                break
            window = words[cursor:cursor + WINDOW]

            # --- 2. segmentation -------------------------------------------------------------
            status.update(cursor=cursor, phrases=k, stage="segmenting"); write_json_atomic(STATUS, status)
            hi = min(MAX_LEN, len(window))
            for i, w in enumerate(window[:hi], start=1):    # never cross a sentence end
                if is_sentence_end(w):
                    hi = i
                    break
            lo = min(MIN_LEN, hi)
            cands = list(range(lo, hi + 1))
            t0 = time.time()
            if len(cands) == 1:
                seg_p, n = [1.0], cands[0]
            else:
                pairs = [(" ".join(window[:c]), SEG_TEMPLATE) for c in cands]
                pe = np.concatenate([jev.enc.predict(pairs[i:i + 16])[:, 1] for i in range(0, len(pairs), 16)])
                seg_p = [float(x) for x in pe]
                def ok_end(c):
                    w = window[c - 1]
                    return w[-1] in ",;:.?!)”’\"" or w.lower().strip("“‘\"'(") not in STOP_END
                allowed = [i for i, c in enumerate(cands) if ok_end(c)] or list(range(len(cands)))
                n = cands[max(allowed, key=lambda i: pe[i])]
            t_seg = time.time() - t0
            phrase = " ".join(window[:n])

            # --- 3. emoji scoring ------------------------------------------------------------
            status.update(stage=f"scoring {len(EMOJI)} emoji", phrase=phrase); write_json_atomic(STATUS, status)
            t0 = time.time()
            pe = score(phrase, emoji_hyps)[:, 1]
            t_emo = time.time() - t0
            lg = np.array([logit(float(p)) for p in pe])

            # --- 4. calibrate + rank ---------------------------------------------------------
            if stats.n >= Z_WARMUP:
                z = (lg - stats.mean) / stats.std()
                key = z
            else:
                z = np.zeros(len(EMOJI)); key = lg
            stats.update(lg)
            order = [int(i) for i in np.argsort(-key) if pe[i] >= MIN_P][:TOP_K]
            top = [dict(e=EMOJI[i][0], name=EMOJI[i][1], p=round(float(pe[i]), 3), z=round(float(z[i]), 2)) for i in order]
            ranked = [int(i) for i in np.argsort(-key)[:8]]
            ev = dict(i=k, w0=cursor, w1=cursor + n, text=phrase, top=top,
                      alts=[dict(e=EMOJI[i][0], name=EMOJI[i][1], p=round(float(pe[i]), 3), z=round(float(z[i]), 2)) for i in ranked],
                      seg=dict(cands=cands, p=[round(x, 3) for x in seg_p], chosen=n),
                      secs_seg=round(t_seg, 2), secs_emoji=round(t_emo, 2))
            out.write(json.dumps(ev, ensure_ascii=False) + "\n"); out.flush()
            with open(LOGITS, "a") as lf:
                lf.write(json.dumps([round(float(x), 3) for x in lg]) + "\n")

            # --- 5. advance ------------------------------------------------------------------
            cursor += n; k += 1; n_phrases += 1
            status.update(cursor=cursor, phrases=k, stage="idle", last_secs=round(t_seg + t_emo, 1))
            write_json_atomic(STATUS, status)
            print(f"[{k:4d}] w{ev['w0']:>5}+{n:<2} seg {t_seg:4.1f}s emoji {t_emo:4.1f}s  "
                  f"{''.join(t['e'] for t in top) or '·':<6} {phrase}", flush=True)

    status.update(state="finished", stage="done"); write_json_atomic(STATUS, status)


if __name__ == "__main__":
    main()
