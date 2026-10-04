"""Score every emoji in our vocab under an LLM: log P(emoji tokens | few-shot prompt), one shared prompt prefill,
candidates batched as suffixes against a broadcast KV cache. Deterministic, no generation.

  /opt/homebrew/opt/mlx-lm/libexec/bin/python lm_emoji.py            # self-test + benchmark
"""
import json, os, sys, time
import numpy as np
import mlx.core as mx
from mlx_lm import load
from mlx_lm.models.cache import make_prompt_cache

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repo root: data/ and models/ live there
sys.path[:0] = [os.path.join(HERE, "experiments"), os.path.join(HERE, "pipelines", "emoji_book")]
sys.path.insert(0, HERE)
from emoji_vocab import EMOJI
from lm_prompt import FEWSHOT


class LMEmoji:
    def __init__(self, model_name, glyphs, fewshot=FEWSHOT):
        self.model, self.tok = load(model_name)
        self.glyphs, self.fewshot = glyphs, fewshot
        cands = [self.tok.encode(" " + g) for g in glyphs]
        self.L = max(len(c) for c in cands)
        self.lens = np.array([len(c) for c in cands])
        self.cand = mx.array([c + [0] * (self.L - len(c)) for c in cands])      # [B, L] right-padded
        self.B = len(glyphs)

    def logprobs(self, phrase):
        prompt = self.fewshot + phrase + "\nEmoji:"
        x = mx.array(self.tok.encode(prompt))[None]
        cache = make_prompt_cache(self.model)
        out = self.model(x, cache=cache)[0, -1].astype(mx.float32)
        first_lp = out - mx.logsumexp(out)                                   # [V] distribution of the 1st emoji token
        lp = first_lp[self.cand[:, 0]]                                       # [B]
        if self.L > 1:
            for c in cache:                                                  # broadcast the shared prefix to all candidates
                T = c.offset
                k, v = c.keys[..., :T, :], c.values[..., :T, :]
                c.keys = mx.broadcast_to(k, (self.B,) + k.shape[1:]); c.values = mx.broadcast_to(v, (self.B,) + v.shape[1:])
            logits = self.model(self.cand[:, :-1], cache=cache).astype(mx.float32)     # [B, L-1, V]
            lps = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
            nxt = self.cand[:, 1:]                                           # tokens predicted at positions 0..L-2
            tok_lp = mx.take_along_axis(lps, nxt[..., None], axis=-1)[..., 0]  # [B, L-1]
            valid = mx.array((np.arange(1, self.L)[None, :] < self.lens[:, None]).astype(np.float32))
            lp = lp + (tok_lp * valid).sum(axis=1)
        mx.eval(lp)
        return np.array(lp)


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
    glyphs = [e for e, _ in EMOJI]
    t = time.time(); lm = LMEmoji(model, glyphs); print(f"load {time.time()-t:.1f}s; max candidate tokens {lm.L}, mean {lm.lens.mean():.1f}", flush=True)
    phrase = "I am literally dying, that was so hilarious."
    # reference: naive full-sequence scoring for a few candidates
    prompt = FEWSHOT + phrase + "\nEmoji:"
    pids = lm.tok.encode(prompt)
    fast = lm.logprobs(phrase)
    for g in ["💀", "😂", "🔥", "🍆"]:
        i = glyphs.index(g) if g in glyphs else None
        if i is None: continue
        ids = pids + lm.tok.encode(" " + g)
        lg = lm.model(mx.array(ids)[None])[0].astype(mx.float32)
        lps = lg - mx.logsumexp(lg, axis=-1, keepdims=True)
        n = len(lm.tok.encode(" " + g)); ref = sum(float(lps[len(pids) - 1 + j, ids[len(pids) + j]]) for j in range(n))
        print(f"selftest {g}: fast {fast[i]:.3f} vs reference {ref:.3f}")
    ts = []
    for _ in range(5):
        t = time.time(); lm.logprobs(phrase); ts.append(time.time() - t)
    print(f"per phrase, all {len(glyphs)} emoji: {np.mean(ts):.2f}s")
