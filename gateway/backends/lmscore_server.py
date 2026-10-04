"""Continuation scoring with an mlx-lm model as a small JSON server: log P(candidate | prompt) for a closed list of candidates.
Deterministic, no generation. Runs in the Homebrew mlx-lm python.

  POST /score {"prompt": str, "candidates": [str, ...]}  ->  {"logprobs": [float, ...]}   (same order as candidates)

The prompt is prefilled once; the candidates are then scored as a batch of suffixes against that prefix's KV cache,
broadcast to the batch. Each candidate is tokenized on its own and appended to the prompt's tokens, so include any
separator (usually a leading space) in the candidate text. Needs a model whose cache is a plain KV cache (Qwen3-Coder;
hybrid linear-attention models are refused).
"""
import argparse, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--port", type=int, required=True)
a = ap.parse_args()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np                                   # noqa: E402
import mlx.core as mx                                # noqa: E402
from mlx_lm import load                              # noqa: E402
from mlx_lm.models.cache import KVCache, make_prompt_cache   # noqa: E402
from _http import serve                              # noqa: E402

MAX_CANDIDATES, MAX_PROMPT_CHARS, BATCH = 4096, 32_000, 512

t0 = time.time()
model, tok = load(a.model)
if not all(isinstance(c, KVCache) for c in make_prompt_cache(model)):
    sys.exit(f"{a.model}: scoring needs a plain KV cache in every layer (hybrid or sliding-window caches are not supported)")
print(f"model loaded in {time.time() - t0:.1f}s", flush=True)

_cands: dict[tuple, tuple] = {}                      # candidate list -> (padded token array, lengths); lists repeat across calls


def encode_candidates(cands: tuple):
    if cands not in _cands:
        ids = [tok.encode(c) for c in cands]
        if not all(ids):
            raise ValueError("every candidate must tokenize to at least one token")
        L = max(len(i) for i in ids)
        _cands.clear()
        _cands[cands] = (np.array([i + [0] * (L - len(i)) for i in ids]), np.array([len(i) for i in ids]))
    return _cands[cands]


def score_batch(prompt_ids: list[int], cand: np.ndarray, lens: np.ndarray) -> np.ndarray:
    cache = make_prompt_cache(model)
    out = model(mx.array(prompt_ids)[None], cache=cache)[0, -1].astype(mx.float32)
    first = out - mx.logsumexp(out)
    c = mx.array(cand)
    lp = first[c[:, 0]]
    B, L = cand.shape
    if L > 1:
        for layer in cache:                          # broadcast the shared prefix to every candidate
            T = layer.offset
            k, v = layer.keys[..., :T, :], layer.values[..., :T, :]
            layer.keys, layer.values = mx.broadcast_to(k, (B,) + k.shape[1:]), mx.broadcast_to(v, (B,) + v.shape[1:])
        logits = model(c[:, :-1], cache=cache).astype(mx.float32)
        lps = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
        tok_lp = mx.take_along_axis(lps, c[:, 1:, None], axis=-1)[..., 0]
        valid = mx.array((np.arange(1, L)[None, :] < lens[:, None]).astype(np.float32))
        lp = lp + (tok_lp * valid).sum(axis=1)
    mx.eval(lp)
    return np.array(lp)


def score(r):
    prompt, cands = r["prompt"], r["candidates"]
    if not isinstance(prompt, str) or not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be a non-empty string of at most {MAX_PROMPT_CHARS} characters")
    if not (isinstance(cands, list) and 0 < len(cands) <= MAX_CANDIDATES and all(isinstance(c, str) and c for c in cands)):
        raise ValueError(f"candidates must be a list of 1 to {MAX_CANDIDATES} non-empty strings")
    cand, lens = encode_candidates(tuple(cands))
    pids = tok.encode(prompt)
    out = np.concatenate([score_batch(pids, cand[i:i + BATCH], lens[i:i + BATCH]) for i in range(0, len(cands), BATCH)])
    return {"logprobs": [round(float(x), 4) for x in out]}


score({"prompt": "warm up", "candidates": [" a", " b"]})
serve(a.port, {"model": a.model, "load_s": round(time.time() - t0, 1)}, {"/score": score})
