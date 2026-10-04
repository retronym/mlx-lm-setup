"""Profiles: named virtual models on top of an existing backend, with request defaults.

A profile is NOT a second process. ``qwen3.6-think`` and ``qwen3.6-fast`` can both point at the one ``qwen3-6-35b-a3b`` backend and
differ only in the defaults the gateway fills into a request (sampling, a token cap, ``chat_template_kwargs`` such as
``enable_thinking``, a system prompt). The rule is fill-only: anything the client sends wins over the profile.

    [profiles.qwen3-6-think]
    backend = "qwen3-6-35b-a3b"
    description = "reasoning on, capped"
    aliases = ["think"]
    system_prompt = "Be concise."
    [profiles.qwen3-6-think.defaults]
    max_tokens = 4096
    temperature = 0.6
    chat_template_kwargs = { enable_thinking = true }
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# Request keys a profile may default, per backend kind. Anything else is rejected so a profile can't smuggle odd fields upstream.
_NUM = (int, float)
ALLOWED: dict[str, dict[str, tuple[type, ...]]] = {
    "llm": {"temperature": _NUM, "top_p": _NUM, "top_k": (int,), "min_p": _NUM, "max_tokens": (int,), "seed": (int,),
            "presence_penalty": _NUM, "frequency_penalty": _NUM, "repetition_penalty": _NUM,
            "stop": (str, list), "chat_template_kwargs": (dict,)},
    "decision": {"temperature": _NUM},
    "nli": {},
}


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    backend: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)
    system_prompt: str | None = None


def parse_profiles(raw: dict, backends: dict) -> dict[str, Profile]:
    """Validate ``[profiles.*]`` against the catalog's backends (a mapping name -> BackendSpec)."""
    out: dict[str, Profile] = {}
    for name, p in raw.items():
        where = f"profiles.{name}"
        if not isinstance(p, dict):
            raise ProfileError(f"{where}: expected a table")
        if not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise ProfileError(f"profile name {name!r}: use letters, digits, '-', '_' or '.'")
        unknown = set(p) - {"backend", "description", "aliases", "defaults", "system_prompt"}
        if unknown:
            raise ProfileError(f"{where}: unknown keys {sorted(unknown)}")
        backend = p.get("backend")
        if backend not in backends:
            raise ProfileError(f"{where}.backend: {backend!r} is not a backend in the catalog")
        kind = backends[backend].kind
        if kind not in ALLOWED:
            raise ProfileError(f"{where}: backend {backend!r} is a {kind!r} backend; profiles support {sorted(ALLOWED)}")
        desc = p.get("description", "")
        if not isinstance(desc, str):
            raise ProfileError(f"{where}.description: expected a string")
        aliases = p.get("aliases", [])
        if not (isinstance(aliases, list) and all(isinstance(a, str) and a for a in aliases)):
            raise ProfileError(f"{where}.aliases: expected a list of non-empty strings")
        sp = p.get("system_prompt")
        if sp is not None and (not isinstance(sp, str) or not sp.strip()):
            raise ProfileError(f"{where}.system_prompt: expected a non-empty string")
        if sp is not None and kind != "llm":
            raise ProfileError(f"{where}.system_prompt: only llm backends take a system prompt")
        defaults = p.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ProfileError(f"{where}.defaults: expected a table")
        allowed = ALLOWED[kind]
        for k, v in defaults.items():
            if k not in allowed:
                raise ProfileError(f"{where}.defaults.{k}: not allowed for {kind} backends (allowed: {sorted(allowed) or 'none'})")
            if isinstance(v, bool) or not isinstance(v, allowed[k]):
                raise ProfileError(f"{where}.defaults.{k}: expected {'/'.join(t.__name__ for t in allowed[k])}, got {type(v).__name__}")
            if k == "stop" and isinstance(v, list) and not all(isinstance(x, str) for x in v):
                raise ProfileError(f"{where}.defaults.stop: expected a string or a list of strings")
            if k == "chat_template_kwargs" and not all(isinstance(x, str) for x in v):
                raise ProfileError(f"{where}.defaults.chat_template_kwargs: keys must be strings")
        out[name] = Profile(name=name, backend=backend, description=desc, aliases=tuple(dict.fromkeys(aliases)),
                            defaults=copy.deepcopy(defaults), system_prompt=sp)
    return out


def apply_defaults(body: dict, profile: Profile | None) -> dict:
    """Return a copy of ``body`` with the profile's defaults filled in. Fill-only: values the client sent (including falsy ones
    like 0 or false) win; ``null`` counts as not sent. ``chat_template_kwargs`` is merged key by key so a client can override
    one flag without losing the profile's others. A system prompt is added only if no system message is present."""
    if profile is None:
        return body
    out = dict(body)
    for k, v in profile.defaults.items():
        if k == "chat_template_kwargs":
            sent = out.get(k)
            merged = copy.deepcopy(v)
            if isinstance(sent, dict):
                merged.update(sent)
            out[k] = merged
        elif out.get(k) is None:
            out[k] = copy.deepcopy(v)
    if profile.system_prompt and isinstance(out.get("messages"), list):
        if not any(isinstance(m, dict) and m.get("role") == "system" for m in out["messages"]):
            out["messages"] = [{"role": "system", "content": profile.system_prompt}, *out["messages"]]
    return out
