"""Logic shared by the HTTP routes and the MCP tools: error mapping, model resolution, lease-and-POST."""
from __future__ import annotations

import time

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
        async with sup.lease(spec.name) as url:
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
