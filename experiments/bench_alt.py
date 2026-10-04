"""Benchmark off-the-shelf typed-decision models on the same 14 phrases as bench_emoji.py.

  .venv-mlxjev/bin/python bench_alt.py mlx2b      # chaoliangUNSW/Jev-Style-2B-Decision-v3-MLX (8-bit), ONE pass, all emoji as options
  .venv-jev/bin/python    bench_alt.py deberta    # com-kotobalabs/open-jev-deberta-v3-large, chunked to fit 512 tokens
  .venv-jev/bin/python    bench_alt.py compare    # agreement with the 4B NLI reference + side-by-side top-3

Writes data/bench_<name>.json: {"model", "phrases", "full": [[score per emoji]], "t_full": [secs per phrase]}
"""
import json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repo root: data/ and models/ live there
sys.path[:0] = [os.path.join(HERE, "experiments"), os.path.join(HERE, "pipelines", "emoji_book")]
DATA = os.path.join(HERE, "data")
sys.path.insert(0, HERE)
from emoji_vocab import EMOJI

QUESTION = "Which emoji best expresses the meaning or mood of this passage?"
NAMES = [n for _, n in EMOJI]
assert len(set(NAMES)) == len(NAMES), "duplicate emoji names"


def save(name, phrases, full, ts, extra=None):
    json.dump(dict(model=name, phrases=phrases, full=full, t_full=ts, **(extra or {})),
              open(os.path.join(DATA, f"bench_{name}.json"), "w"))


def run_mlx2b():
    sys.path.insert(0, os.path.join(HERE, "models", "jevstyle-2b-mlx"))
    from jev_style_decision_mlx import JevStyleDecisionMLX
    import mlx.core as mx
    phrases = json.load(open(os.path.join(DATA, "bench_phrases.json")))
    t = time.time()
    m = JevStyleDecisionMLX(os.path.join(HERE, "models", "jevstyle-2b-mlx"), precision="8bit")
    print(f"load {time.time()-t:.1f}s", flush=True)
    m.decide(phrases[0], QUESTION, options=NAMES)             # warm-up
    full, ts = [], []
    for ph in phrases:
        t = time.time()
        r = m.decide(ph, QUESTION, options=NAMES)
        dt = time.time() - t
        full.append([r["scores"][n] for n in NAMES]); ts.append(dt)
        print(f"{dt:5.2f}s  in={r['input_tokens']} tok  top={r['answer']!r:28} | {ph}", flush=True)
    save("jevstyle-2b-mlx", phrases, full, ts)


def run_deberta():
    import torch
    sys.path.insert(0, os.path.join(HERE, "models", "deberta-openjev"))
    from typed_decisions.open_jev import OpenJev
    phrases = json.load(open(os.path.join(DATA, "bench_phrases.json")))
    t = time.time()
    m = OpenJev.from_pretrained(os.path.join(HERE, "models", "deberta-openjev"))
    print(f"load {time.time()-t:.1f}s on {m.device}", flush=True)
    col = m.collator
    opt_len = [len(col._ids(n)) + 1 for n in NAMES]
    q_len = len(col._ids(QUESTION)) + 1

    def chunks(state_len):
        out, cur, used = [], [], state_len + q_len + 3
        for i, L in enumerate(opt_len):
            if used + L > m.config.get("max_len", 512) - 2 or len(cur) >= 255:
                out.append(cur); cur, used = [], state_len + q_len + 3
            cur.append(i); used += L
        out.append(cur)
        return out

    @torch.no_grad()
    def score(state):
        sl = min(len(col._ids(state)), 256)
        scores = np.zeros(len(NAMES))
        for idx in chunks(sl):
            q = OpenJev._question(0, {"type": "choice", "instructions": QUESTION, "options": [NAMES[i] for i in idx]})
            b = col([(state, [q])], m.device)
            lg = m.model(b["input_ids"], b["attention_mask"], b["opt_pos"], b["opt_mask"], b["q_pos"], b["seg"]).float()
            scores[idx] = lg[0, 0, :len(idx)].cpu().numpy()
        return scores

    score(phrases[0])
    full, ts = [], []
    for ph in phrases:
        t = time.time(); s = score(ph); dt = time.time() - t
        full.append(s.tolist()); ts.append(dt)
        print(f"{dt:5.2f}s  top={NAMES[int(np.argmax(s))]!r:28} | {ph}", flush=True)
    save("deberta-openjev", phrases, full, ts)


def compare():
    ref = json.load(open(os.path.join(DATA, "bench_qwen3.5-4b-nli-v5.json")))
    R = {}
    for f in sorted(os.listdir(DATA)):
        if f.startswith("bench_") and f.endswith(".json") and f != "bench_phrases.json":
            d = json.load(open(os.path.join(DATA, f))); R[d["model"]] = d
    refp = np.array(ref["full"])
    top = lambda p, k=3: set(np.argsort(-np.asarray(p))[:k].tolist())
    print(f"{'model':28s} {'s/phrase':>9s} {'top3 ovl':>9s} {'top1 =':>7s}   (vs 4B NLI full-vocab reference)")
    for n, d in R.items():
        p = np.array(d["full"])
        ov = np.mean([len(top(a) & top(b)) / 3 for a, b in zip(p, refp)])
        t1 = np.mean([int(np.argmax(a) == np.argmax(b)) for a, b in zip(p, refp)])
        print(f"{n:28s} {np.mean(d['t_full']):9.2f} {ov:9.2f} {t1:7.2f}")
    print()
    for i, ph in enumerate(ref["phrases"]):
        row = " | ".join(f"{n.split('-')[0][:8]:8s} " + "".join(EMOJI[j][0] for j in np.argsort(-np.array(d['full'][i]))[:3]) for n, d in R.items())
        print(f"{ph[:44]:44s} {row}")


if __name__ == "__main__":
    {"mlx2b": run_mlx2b, "deberta": run_deberta, "compare": compare}[sys.argv[1]]()
