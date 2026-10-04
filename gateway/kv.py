"""KV-cache size estimates from a model's ``config.json``.

Weights are only part of a model's memory: the KV cache grows with the context. Modern models differ a lot in how much, because
only some layers keep a per-token cache:

* ``full_attention``    keeps K and V for every token (2 x kv_heads x head_dim per token per layer)
* ``sliding_attention`` keeps at most ``sliding_window`` tokens, so it stops growing
* ``linear_attention``  (gated delta-net / Mamba style) keeps a small constant recurrent state: counted as 0 here
* MLA (DeepSeek style, ``kv_lora_rank``) keeps one compressed latent per token per layer instead of K and V

This is an ESTIMATE: it ignores the runtime's prompt-cache entries, activation peaks during prefill, and allocator slack.
Measured peaks in docs/MODEL_GUIDE.md are within about 1 GB of weights + KV(6k tokens) for the models checked.
"""
from __future__ import annotations

GROUP = 64                # mlx quantized KV: one fp16 scale and one fp16 bias per group of 64 elements
GIB = 1024 ** 3


class KVEstimateError(ValueError):
    pass


def _text(cfg: dict) -> dict:
    return cfg.get("text_config") or cfg


def bytes_per_element(kv_bits: int | None) -> float:
    if kv_bits is None or kv_bits >= 16:
        return 2.0
    if kv_bits not in (2, 3, 4, 5, 6, 8):
        raise KVEstimateError(f"unsupported kv_bits {kv_bits}")
    return kv_bits / 8 + 4 / GROUP


def layer_plan(cfg: dict) -> list[dict]:
    """One entry per layer: {'kind': full|sliding|linear, 'per_token': bytes per token at fp16, 'window': int | None}."""
    t = _text(cfg)
    try:
        n = int(t["num_hidden_layers"])
    except (KeyError, TypeError, ValueError):
        raise KVEstimateError("config has no num_hidden_layers") from None
    types = t.get("layer_types")
    if not types:
        interval = t.get("full_attention_interval")
        types = [("full_attention" if (i + 1) % interval == 0 else "linear_attention") for i in range(n)] if interval else ["full_attention"] * n
    if len(types) != n:
        raise KVEstimateError("layer_types length does not match num_hidden_layers")
    plan = []
    for lt in types:
        if lt in ("linear_attention", "mamba", "recurrent"):
            plan.append({"kind": "linear", "per_token": 0.0, "window": None})
            continue
        if "kv_lora_rank" in t:                                       # MLA: one latent (+ rope part) per token, not K and V
            per = float(t["kv_lora_rank"] + t.get("qk_rope_head_dim", 0)) * 2.0
            plan.append({"kind": "full", "per_token": per, "window": None})
            continue
        sliding = lt in ("sliding_attention", "local")
        heads = t.get("num_key_value_heads") if sliding else (t.get("num_global_key_value_heads") or t.get("num_key_value_heads"))
        dim = t.get("head_dim") if sliding else (t.get("global_head_dim") or t.get("head_dim"))
        if heads is None:
            heads = t.get("num_attention_heads")
        if dim is None and t.get("hidden_size") and t.get("num_attention_heads"):
            dim = t["hidden_size"] // t["num_attention_heads"]
        if not heads or not dim:
            raise KVEstimateError("cannot determine kv heads / head dim from config")
        window = int(t["sliding_window"]) if sliding and t.get("sliding_window") else None
        plan.append({"kind": "sliding" if sliding else "full", "per_token": 2.0 * heads * dim * 2.0, "window": window})
    return plan


def kv_bytes(cfg: dict, context_tokens: int, kv_bits: int | None = None) -> float:
    """Estimated KV cache bytes for a context of ``context_tokens`` tokens."""
    if context_tokens < 0:
        raise KVEstimateError("context_tokens must be >= 0")
    scale = bytes_per_element(kv_bits) / 2.0
    total = 0.0
    for layer in layer_plan(cfg):
        toks = min(context_tokens, layer["window"]) if layer["window"] else context_tokens
        total += layer["per_token"] * scale * toks
    return total


def kv_gb(cfg: dict, context_tokens: int, kv_bits: int | None = None) -> float:
    return kv_bytes(cfg, context_tokens, kv_bits) / 1e9


def kv_bytes_per_token(cfg: dict, kv_bits: int | None = None) -> float:
    """Marginal bytes per additional token once any sliding windows are full (i.e. the long-context slope)."""
    scale = bytes_per_element(kv_bits) / 2.0
    return sum(l["per_token"] * scale for l in layer_plan(cfg) if not l["window"])


def max_context(cfg: dict) -> int | None:
    v = _text(cfg).get("max_position_embeddings")
    return int(v) if isinstance(v, int) else None
