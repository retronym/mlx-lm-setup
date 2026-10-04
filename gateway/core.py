"""Logic shared by the HTTP routes and the MCP tools: error mapping, model resolution, lease-and-POST."""
from __future__ import annotations

import time

import httpx

from .catalog import BackendSpec, Catalog
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
