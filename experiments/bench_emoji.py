"""Benchmark emoji-scoring strategies across OpenJev checkpoints.

  1) python bench_emoji.py prepare                      # chunk the book with spaCy, pick sample phrases
  2) JEV_SUBFOLDER=qwen3.5-4b-nli-v5 python bench_emoji.py run   (repeat per checkpoint; one process each)
  3) python bench_emoji.py report                       # compare against the 4B full-vocab reference

`run` stores, per phrase, P(entail) for all 343 emoji and for the 9 groups, plus wall times.
Two-stage (top-g groups -> only those groups' emoji) is simulated from the stored scores.
"""
import json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repo root: data/ and models/ live there
sys.path[:0] = [os.path.join(HERE, "experiments"), os.path.join(HERE, "pipelines", "emoji_book")]
DATA = os.path.join(HERE, "data")
PHRASES = os.path.join(DATA, "bench_phrases.json")
T = "This passage is about {}."


def prepare(n=14, stride=7):
    import chunker_spacy
    words = [w["w"] for w in json.load(open(os.path.join(DATA, "book_words.json")))]
    cur, phrases = 0, []
    t0 = time.time()
    while cur < 800 and len(phrases) < n * stride:
        k = chunker_spacy.next_len(words[cur:cur + 32])
        phrases.append(" ".join(words[cur:cur + k])); cur += k
    picked = phrases[::stride][:n]
    json.dump(picked, open(PHRASES, "w"), ensure_ascii=False, indent=1)
    print(f"chunked {len(phrases)} phrases in {time.time()-t0:.1f}s; picked {len(picked)}")
    for p in picked:
        print(" |", p)


def run():
    import torch
    from emoji_vocab import EMOJI, GROUPS
    from jev_check import Jev, SUBFOLDER
    phrases = json.load(open(PHRASES))
    jev = Jev()
    hy_e = [T.format(n) for _, n in EMOJI]
    hy_g = [T.format(g) for g in GROUPS]

    def score(prem, hyps, chunk=64):
        out = [jev.enc.predict_hypotheses(prem, hyps[i:i + chunk]) for i in range(0, len(hyps), chunk)]
        if jev.enc.device == "mps":
            torch.mps.synchronize()
        return np.concatenate(out)[:, 1]

    score(phrases[0], hy_e[:8])                       # warm-up
    res = dict(model=SUBFOLDER, phrases=phrases, full=[], group=[], t_full=[], t_group=[])
    for i, ph in enumerate(phrases):
        t = time.time(); g = score(ph, hy_g); tg = time.time() - t
        t = time.time(); f = score(ph, hy_e); tf = time.time() - t
        res["full"].append(f.tolist()); res["group"].append(g.tolist()); res["t_full"].append(tf); res["t_group"].append(tg)
        print(f"[{SUBFOLDER}] {i+1}/{len(phrases)} full {tf:.1f}s group {tg:.1f}s | {ph}", flush=True)
    json.dump(res, open(os.path.join(DATA, f"bench_{SUBFOLDER}.json"), "w"))


def report():
    from emoji_vocab import EMOJI, GROUP_OF
    files = sorted(f for f in os.listdir(DATA) if f.startswith("bench_") and f.endswith(".json") and f != "bench_phrases.json")
    R = {json.load(open(os.path.join(DATA, f)))["model"]: json.load(open(os.path.join(DATA, f))) for f in files}
    ref_name = "qwen3.5-4b-nli-v5"
    ref = np.array(R[ref_name]["full"])
    top = lambda p, k=3: set(np.argsort(-p)[:k].tolist())
    ref_top = [top(p) for p in ref]
    gof = np.array(GROUP_OF)
    print(f"reference: {ref_name}, full vocab ({len(EMOJI)}), {len(ref)} phrases; metric = overlap of top-3 emoji sets (and top-1 match) vs reference\n")
    print(f"{'config':34s} {'s/phrase':>9s} {'top3 ovl':>9s} {'top1 =':>7s}")
    for name, r in R.items():
        full, grp = np.array(r["full"]), np.array(r["group"])
        tf, tg = np.mean(r["t_full"]), np.mean(r["t_group"])
        short = name.replace("qwen3.5-", "").replace("-nli", "")
        ovl = np.mean([len(top(f) & rt) / 3 for f, rt in zip(full, ref_top)])
        t1 = np.mean([int(np.argmax(f) == np.argmax(rr)) for f, rr in zip(full, ref)])
        print(f"{short + ' full':34s} {tf:9.1f} {ovl:9.2f} {t1:7.2f}")
        for g in (2, 3, 4):
            ovs, t1s, fr = [], [], []
            for f, gp, rt, rr in zip(full, grp, ref_top, ref):
                keep = np.isin(gof, np.argsort(-gp)[:g])
                masked = np.where(keep, f, -1)
                ovs.append(len(top(masked) & rt) / 3); t1s.append(int(np.argmax(masked) == np.argmax(rr))); fr.append(keep.mean())
            tt = tg + tf * np.mean(fr)
            print(f"{short + f' + top-{g} groups':34s} {tt:9.1f} {np.mean(ovs):9.2f} {np.mean(t1s):7.2f}   (scores {np.mean(fr)*len(EMOJI):.0f} emoji)")
    # cascade: small model scores all emoji, the 4B re-ranks only the small model's top-K
    big = np.array(R[ref_name]["full"]); tbig = np.mean(R[ref_name]["t_full"])
    for name, r in R.items():
        if name == ref_name:
            continue
        small, ts = np.array(r["full"]), np.mean(r["t_full"])
        short = name.replace("qwen3.5-", "").replace("-nli", "")
        for K in (20, 40, 80):
            ovs, t1s = [], []
            for s, b, rt, rr in zip(small, big, ref_top, ref):
                keep = np.zeros(len(s), bool); keep[np.argsort(-s)[:K]] = True
                masked = np.where(keep, b, -1)
                ovs.append(len(top(masked) & rt) / 3); t1s.append(int(np.argmax(masked) == np.argmax(rr)))
            print(f"{'cascade ' + short + ' -> 4b top-' + str(K):34s} {ts + tbig * K / len(EMOJI):9.1f} {np.mean(ovs):9.2f} {np.mean(t1s):7.2f}")


if __name__ == "__main__":
    {"prepare": prepare, "run": run, "report": report}[sys.argv[1]]()
