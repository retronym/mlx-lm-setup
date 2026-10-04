"""Rich, human-oriented details for the chat site's model picker: what each entry is, what it costs, how it behaves.

Pure functions over the catalog, the supervisor's snapshot and (optionally) discovery results, so they are easy to test. The lean
OpenAI-compatible ``/v1/models`` is untouched; this feeds ``GET /api/models``.
"""
from __future__ import annotations

import json
from typing import Any

from .catalog import BackendSpec, Catalog
from .profiles import Profile


def thinking_default(spec: BackendSpec, profile: Profile | None = None) -> str | None:
    """'on' / 'off' when the catalog sets it (a profile's chat_template_kwargs, else the backend's --chat-template-args), else None
    (the model's own default applies; the gateway does not know it)."""
    if profile is not None:
        v = (profile.defaults.get("chat_template_kwargs") or {}).get("enable_thinking")
        if isinstance(v, bool):
            return "on" if v else "off"
    args = spec.options.get("args") or []
    for i, a in enumerate(args):
        raw = None
        if a == "--chat-template-args" and i + 1 < len(args):
            raw = args[i + 1]
        elif a.startswith("--chat-template-args="):
            raw = a.split("=", 1)[1]
        if raw is not None:
            try:
                v = json.loads(raw).get("enable_thinking")
            except (ValueError, AttributeError):
                continue
            if isinstance(v, bool):
                return "on" if v else "off"
    return None


def _memory(spec: BackendSpec) -> dict[str, Any]:
    return {"est_gb": spec.est_mem_gb, "weights_gb": spec.weights_gb, "kv_gb": spec.kv_gb, "overhead_gb": spec.overhead_gb,
            "context_tokens": spec.context_tokens}


def _disk(spec: BackendSpec, discovered: dict | None) -> dict[str, Any] | None:
    f = (discovered or {}).get(spec.options.get("model", ""))
    if f is None:
        return None
    return {"weights_gb": f.weights_gb, "quant_bits": f.quant_bits, "model_type": f.model_type,
            "kv_kib_per_token": f.kv_kib_per_token, "max_context": f.max_context}


def _runtime(snap: dict | None) -> dict[str, Any]:
    s = snap or {}
    return {"state": s.get("state", "stopped"), "ready": s.get("state") == "ready", "busy": bool(s.get("busy")),
            "idle_s": s.get("idle_s"), "ttl_s": s.get("ttl_s"), "ttl_left_s": s.get("ttl_left_s"), "pinned": bool(s.get("pinned")),
            "last_start_s": s.get("last_start_s"), "requests": s.get("requests", 0)}


def build_entries(catalog: Catalog, snapshot: dict[str, dict], discovered: dict | None = None) -> list[dict]:
    """One entry per LLM backend, then one per profile whose backend is an LLM. ``snapshot`` maps backend name -> its
    supervisor snapshot row; ``discovered`` maps an HF model id -> discovery.Found (optional)."""
    out: list[dict] = []
    profiles_of: dict[str, list[str]] = {}
    for p in catalog.profiles.values():
        profiles_of.setdefault(p.backend, []).append(p.name)
    for spec in catalog.backends.values():
        if spec.kind != "llm":
            continue
        out.append({
            "id": spec.name, "type": "backend", "backend": spec.name, "kind": spec.kind, "description": spec.description,
            "model": spec.options.get("model"), "aliases": [a for a in spec.aliases if a != spec.options.get("model")],
            "memory": _memory(spec), "disk": _disk(spec, discovered), "thinking": thinking_default(spec),
            "defaults": {}, "system_prompt": False, "profiles": profiles_of.get(spec.name, []),
            **_runtime(snapshot.get(spec.name))})
    for p in catalog.profiles.values():
        spec = catalog.backends[p.backend]
        if spec.kind != "llm":
            continue
        out.append({
            "id": p.name, "type": "profile", "backend": p.backend, "kind": spec.kind, "description": p.description,
            "model": spec.options.get("model"), "aliases": list(p.aliases),
            "memory": _memory(spec), "disk": _disk(spec, discovered), "thinking": thinking_default(spec, p),
            "defaults": p.defaults, "system_prompt": bool(p.system_prompt), "profiles": [],
            **_runtime(snapshot.get(p.backend))})
    return out
