"""Logic shared by the HTTP routes and the MCP tools: error mapping, model resolution, lease-and-POST."""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from .catalog import BackendSpec, Catalog
from .profiles import Profile
from .supervisor import BackendUnavailable, InsufficientMemory, StartFailed, Supervisor, UnknownBackend

COLD_HEADER_THRESHOLD_S = 0.05


class ApiError(Exception):
    def __init__(self, status: int, kind: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.kind, self.message, self.headers = status, kind, message, headers or {}


def map_errors(e: Exception) -> ApiError:
    if isinstance(e, ApiError):
        return e
    if isinstance(e, BackendUnavailable):
        return ApiError(503, "backend_unavailable", str(e), {"Retry-After": "5"})
    if isinstance(e, InsufficientMemory):
        return ApiError(503, "insufficient_memory", str(e), {"Retry-After": "10"})
    if isinstance(e, StartFailed):
        return ApiError(503, "backend_start_failed", str(e))
    if isinstance(e, UnknownBackend):
        return ApiError(404, "model_not_found", str(e))
    if isinstance(e, httpx.HTTPError):
        return ApiError(502, "backend_error", f"backend request failed: {type(e).__name__}: {e}")
    return ApiError(500, "gateway_error", f"{type(e).__name__}: {e}")


def resolve(catalog: Catalog, model: str | None, kind: str) -> BackendSpec:
    try:
        return catalog.find(model, kind)
    except KeyError as e:
        raise ApiError(404, "model_not_found", str(e.args[0])) from None
    except ValueError as e:
        raise ApiError(400, "wrong_model_kind", str(e)) from None


def resolve_target(catalog: Catalog, model: str | None, kind: str) -> tuple[BackendSpec, Profile | None]:
    """Like ``resolve`` but also returns the profile when ``model`` names one (apply it with ``profiles.apply_defaults``)."""
    try:
        return catalog.resolve(model, kind)
    except KeyError as e:
        raise ApiError(404, "model_not_found", str(e.args[0])) from None
    except ValueError as e:
        raise ApiError(400, "wrong_model_kind", str(e)) from None


async def post_json(sup: Supervisor, client: httpx.AsyncClient, spec: BackendSpec, path: str, body) -> tuple[httpx.Response, dict]:
    """Lease the backend (lazy start, queue, idle-clock reset), POST ``body`` as JSON, return (response, meta)."""
    t0 = time.monotonic()
    try:
        async with sup.lease(spec.name, path) as url:
            cold = time.monotonic() - t0
            r = await client.post(url + path, json=body)
    except Exception as e:                                           # noqa: BLE001
        raise map_errors(e) from e
    return r, {"backend": spec.name, "cold_start_s": round(cold, 2) if cold > COLD_HEADER_THRESHOLD_S else 0.0}


# ---- lifecycle operations shared by the HTTP API and the MCP tools ---------------------------------------------------------
_COMPACT = ("name", "kind", "state", "busy", "est_mem_gb", "holds_memory", "ttl_s", "ttl_left_s", "idle_s", "pinned",
            "requests", "starts", "passivations", "last_start_s", "last_error")


def compact_backend(s: dict) -> dict:
    return {k: s[k] for k in _COMPACT if k in s}


def _snap(sup: Supervisor, name: str) -> dict:
    return compact_backend(next(s for s in sup.snapshot() if s["name"] == name))


def _runtime(sup: Supervisor, name: str):
    try:
        return sup._get(name)
    except UnknownBackend as e:
        raise ApiError(404, "model_not_found", str(e)) from None


async def op_start(sup: Supervisor, name: str) -> dict:
    _runtime(sup, name)
    try:
        await sup.start(name)
    except Exception as e:                                           # noqa: BLE001
        raise map_errors(e) from e
    return _snap(sup, name)


async def op_stop(sup: Supervisor, name: str, force: bool = False) -> dict:
    _runtime(sup, name)
    try:
        await sup.stop(name, force=force)
    except Exception as e:                                           # noqa: BLE001
        raise map_errors(e) from e
    return _snap(sup, name)


def op_policy(catalog: Catalog, sup: Supervisor, name: str, ttl_s: float | None = None, pinned: bool | None = None) -> dict:
    b = _runtime(sup, name)
    if ttl_s is not None and (isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)) or ttl_s < 0):
        raise ApiError(400, "invalid_arguments", "ttl_s must be a number >= 0 (0 = never passivate)")
    if pinned is not None and not isinstance(pinned, bool):
        raise ApiError(400, "invalid_arguments", "pinned must be true or false")
    if pinned and not b.pinned:
        total = sum(r.spec.est_mem_gb for r in sup.rt.values() if r.pinned or r is b)
        if total > catalog.gateway.memory_budget_gb:
            raise ApiError(400, "invalid_arguments", f"pinning {name} would pin {total:g} GB, more than the "
                                                     f"{catalog.gateway.memory_budget_gb:g} GB budget")
    sup.set_policy(name, ttl_s=ttl_s, pinned=pinned)
    return _snap(sup, name)


async def status_payload(catalog: Catalog, sup: Supervisor) -> dict:
    """Everything the admin site and /api/backends show: backends, memory budget, system reading, pressure thresholds."""
    mem = sup.memory_status()
    g = catalog.gateway
    return {"backends": sup.snapshot(), "gateway": {
        "port": g.port, "memory_budget_gb": mem["budget_gb"], "resident_est_gb": mem["resident_gb"], "free_est_gb": mem["free_gb"],
        "default_llm": g.default_llm, "system": await sup.system_status(),
        "pressure": {"enabled": g.pressure_eviction, "min_free_pct": g.min_free_pct, "swap_growth_gb": g.swap_growth_gb}}}


# ---- speech helpers shared by the HTTP API and the MCP tools ----------------------------------------------------------------
def voices_listing(catalog: Catalog) -> list[dict]:
    """Each speech model with its preset voices and the named reference clips it can clone (with their transcripts)."""
    out = []
    for s in catalog.backends.values():
        if s.kind != "tts":
            continue
        refs = Path(s.options["refs_dir"]) if s.options.get("refs_dir") else None
        clips = sorted(refs.glob("*.wav")) if refs and refs.is_dir() else []
        out.append({"model": s.name, "aliases": list(s.aliases), "description": s.description, "default_voice": s.options.get("voice"),
                    "voices": s.options.get("voices", []), "default_ref": s.options.get("ref_audio"),
                    "reference_clips": [p.stem for p in clips],
                    "reference_transcripts": {p.stem: (p.with_suffix(".txt").read_text().strip() if p.with_suffix(".txt").exists() else None)
                                              for p in clips}})
    return out


def default_narrator(catalog: Catalog) -> str | None:
    """The speech model narration uses by default: the first one with a default reference clip that exists (a cloned narrator,
    the same voice in every scene), else None (the catalog's default speech model)."""
    for s in catalog.backends.values():
        ref, refs = s.options.get("ref_audio"), s.options.get("refs_dir")
        if s.kind == "tts" and ref and refs and (Path(refs) / f"{ref}.wav").exists():
            return s.name
    return None


async def run_narrate(catalog: Catalog, sup: Supervisor, client: httpx.AsyncClient, scenes, *, model: str | None = None,
                      voice: str | None = None, ref_audio: str | None = None, speed: float = 1.0, fresh: bool = False) -> dict:
    from .narrate import NarrateError, narrate, parse_scenes
    from .profiles import apply_defaults
    try:
        parsed = parse_scenes(scenes)
    except NarrateError as e:
        raise ApiError(400, "invalid_arguments", str(e)) from None
    tts, prof = resolve_target(catalog, model or default_narrator(catalog), "tts")
    stt = resolve(catalog, None, "stt")

    async def post(spec, path, body):
        r, _ = await post_json(sup, client, spec, path, body)
        if r.status_code != 200:
            raise ApiError(400 if 400 <= r.status_code < 500 else 502, "backend_error", f"{spec.name} returned {r.status_code}: {r.text[:300]}")
        return r.json()

    voice_args = {k: v for k, v in dict(voice=voice, ref_audio=ref_audio, speed=speed, fresh=fresh or None).items() if v is not None}
    res = await narrate(parsed, lambda b: post(tts, "/speak", apply_defaults(b, prof)), lambda b: post(stt, "/transcribe", b), voice_args)
    return {**res, "model": tts.name, "stt_model": stt.name}


# ---- vision helpers shared by the HTTP API and the MCP tools ----------------------------------------------------------------
def vision_models(catalog: Catalog) -> list[str]:
    return [s.name for s in catalog.backends.values() if s.kind == "vision"]


def vision_target(catalog: Catalog, model: str | None) -> tuple[BackendSpec, Profile | None] | None:
    """(backend, profile) if `model` names a vision backend or a profile of one, else None."""
    if not model:
        return None
    try:
        return catalog.resolve(model, "vision")
    except (KeyError, ValueError):
        return None


async def run_look(catalog: Catalog, sup: Supervisor, client: httpx.AsyncClient, *, model: str | None = None, max_tokens: int = 1024,
                   temperature: float = 0.0, **kw) -> dict:
    """`look` against a vision backend; kw as for vision.look (images, prompt or preset, context, system, each, gates, image_tokens, max_attempts)."""
    from .gates import GateSpecError, parse_gates
    from .iterate import run_iterate
    from .profiles import apply_defaults
    from .vision import look
    spec, prof = resolve_target(catalog, model, "vision")

    async def chat(msgs: list[dict], image_tokens: int | None) -> tuple[str, dict]:
        body = {"messages": msgs, "max_tokens": max_tokens, "temperature": temperature, **({"image_tokens": image_tokens} if image_tokens else {})}
        r, meta = await post_json(sup, client, spec, "/v1/chat/completions", apply_defaults(body, prof))
        if r.status_code != 200:
            raise ApiError(400 if 400 <= r.status_code < 500 else 502, "backend_error", f"{spec.name} returned {r.status_code}: {r.text[:300]}")
        d = r.json()
        return (d["choices"][0]["message"].get("content") or ""), {"model": spec.name, "usage": d.get("usage"),
                                                                     "secs": (d.get("x_timing") or {}).get("secs")}

    async def iterate(gates: list[dict], msgs: list[dict], image_tokens: int | None, max_attempts: int) -> dict:
        try:
            parsed = parse_gates(gates)
        except GateSpecError as e:
            raise ApiError(400, "invalid_arguments", str(e)) from None
        nli = resolve(catalog, None, "nli") if any(g.type == "nli" for g in parsed) else None

        async def entail(premise: str, hyps: list[str]) -> list[dict]:
            r, _ = await post_json(sup, client, nli, "/entail", {"premise": premise, "hypotheses": hyps})
            if r.status_code != 200:
                raise ApiError(502, "backend_error", f"{nli.name} returned {r.status_code}: {r.text[:300]}")
            d = r.json()
            return [dict(zip(d["labels"], p)) for p in d["probs"]]

        return await run_iterate(lambda _override, convo: chat(convo, image_tokens), entail, parsed, msgs, max_attempts)

    if not 1 <= int(kw.get("max_attempts", 2)) <= 6:
        raise ApiError(400, "invalid_arguments", "max_attempts must be 1..6")
    return {**await look(chat, iterate, **kw), "model": spec.name}


# ---- translate (HTTP API and MCP tool) ----------------------------------------------------------------------------------------
async def run_translate(catalog: Catalog, sup: Supervisor, client: httpx.AsyncClient, **kw) -> dict:
    """kw as for translate.translate: text | image, mode, model, max_attempts."""
    from .gates import parse_gates
    from .iterate import run_iterate
    from .translate import translate

    async def iterate(name: str, gates: list[dict], msgs: list[dict], max_attempts: int) -> dict:
        spec = catalog.backends[name]
        cold = [0.0]

        async def chat(_override, convo: list[dict]) -> tuple[str, dict]:
            body = {"messages": convo, "max_tokens": 4096, "temperature": 0.0}
            if spec.kind == "llm":
                body["model"] = spec.options.get("model", spec.name)          # mlx_lm.server may try to load a different model id
            r, meta = await post_json(sup, client, spec, "/v1/chat/completions", body)
            cold[0] += meta["cold_start_s"]
            if r.status_code != 200:
                raise ApiError(400 if 400 <= r.status_code < 500 else 502, "backend_error", f"{spec.name} returned {r.status_code}: {r.text[:300]}")
            d = r.json()
            return (d["choices"][0]["message"].get("content") or ""), {"model": spec.name, "secs": (d.get("x_timing") or {}).get("secs")}

        res = await run_iterate(chat, None, parse_gates(gates), msgs, max_attempts)
        return {**res, "cold_start_s": round(cold[0], 2)}

    return await translate(iterate, catalog, sup.snapshot(), **kw)
