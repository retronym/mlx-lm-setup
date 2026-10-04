"""The gateway's HTTP front door (Starlette): OpenAI-compatible chat, typed decisions, NLI, read-only status.

Every proxied request goes through ``Supervisor.lease`` so the backend is started lazily, requests queue per backend, and a
backend is never passivated while a request (including a streaming response) is in flight.
"""
from __future__ import annotations

import contextlib
import json
import time
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .catalog import Catalog
from .supervisor import BackendUnavailable, GatewayError, InsufficientMemory, StartFailed, State, Supervisor, UnknownBackend

LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}
COLD_HEADER_THRESHOLD_S = 0.05


class ApiError(Exception):
    def __init__(self, status: int, kind: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.kind, self.message, self.headers = status, kind, message, headers or {}


def error_response(status: int, kind: str, message: str, headers: dict | None = None) -> JSONResponse:
    """OpenAI-style error body so standard clients show something sensible."""
    return JSONResponse({"error": {"message": message, "type": kind, "code": kind}}, status_code=status, headers=headers)


# ---- Host / Origin policy -----------------------------------------------------------------------------------------------
class LocalOnly:
    """Pure-ASGI middleware (does not buffer streaming bodies).

    * Host must be a loopback name WITH the gateway's port  -> otherwise 421 (blocks DNS rebinding).
    * Origin, if present, must be a loopback origin          -> otherwise 403 (blocks other websites in the browser).
      Allowed origins get CORS headers; OPTIONS preflights are answered here.
    The MCP SDK applies a similar Host check to /mcp only; this covers every route.
    """

    def __init__(self, app, port: int):
        self.app, self.port = app, port

    def host_ok(self, host: str) -> bool:
        name, _, port = host.rpartition(":") if not host.endswith("]") else (host, "", "")
        return name.lower() in LOOPBACK and port == str(self.port)

    @staticmethod
    def origin_ok(origin: str) -> bool:
        try:
            u = urlsplit(origin)
        except ValueError:
            return False
        host = f"[{u.hostname}]" if u.hostname and ":" in u.hostname else (u.hostname or "")
        return u.scheme in ("http", "https") and host in LOOPBACK

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        h = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        if not self.host_ok(h.get("host", "")):
            return await error_response(421, "misdirected_request", "Host not allowed (gateway is localhost-only)")(scope, receive, send)
        origin = h.get("origin")
        cors: list[tuple[bytes, bytes]] = []
        if origin is not None:
            if not self.origin_ok(origin):
                return await error_response(403, "forbidden_origin", "Origin not allowed")(scope, receive, send)
            cors = [(b"access-control-allow-origin", origin.encode()), (b"vary", b"Origin")]
            if scope["method"] == "OPTIONS":
                return await Response(status_code=204, headers={
                    "Access-Control-Allow-Origin": origin, "Vary": "Origin",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "content-type, authorization, x-gateway-token",
                    "Access-Control-Max-Age": "600"})(scope, receive, send)

        async def send_cors(message):
            if message["type"] == "http.response.start" and cors:
                message = {**message, "headers": [*message.get("headers", []), *cors]}
            await send(message)

        await self.app(scope, receive, send_cors)


# ---- the app ------------------------------------------------------------------------------------------------------------
def create_app(catalog: Catalog, supervisor: Supervisor | None = None, *, read_timeout_s: float = 600.0) -> Starlette:
    state: dict = {"sup": supervisor, "client": None, "owns_sup": supervisor is None}

    def sup() -> Supervisor:
        return state["sup"]

    def client() -> httpx.AsyncClient:
        if state["client"] is None:
            state["client"] = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(connect=5.0, read=read_timeout_s, write=60.0, pool=None))
        return state["client"]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if state["sup"] is None:
            state["sup"] = Supervisor(catalog)
        await sup().start_background()
        try:
            yield
        finally:
            if state["owns_sup"]:
                await sup().shutdown()
            if state["client"] is not None:
                await state["client"].aclose()
                state["client"] = None

    def resolve(model: str | None, kind: str):
        try:
            return catalog.find(model, kind)
        except KeyError as e:
            raise ApiError(404, "model_not_found", str(e.args[0])) from None
        except ValueError as e:
            raise ApiError(400, "wrong_model_kind", str(e)) from None

    async def read_json(request: Request) -> dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "invalid_json", "request body is not valid JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")
        return body

    def map_errors(e: Exception) -> ApiError:
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

    def meta_headers(name: str, cold_s: float) -> dict:
        h = {"X-Gateway-Backend": name}
        if cold_s > COLD_HEADER_THRESHOLD_S:
            h["X-Gateway-Cold-Start-Secs"] = f"{cold_s:.2f}"
        return h

    async def forward(spec, path: str, body: dict, *, stream: bool = False) -> Response:
        """Lease the backend, POST ``body`` to ``path``. Non-streaming: return the buffered reply. Streaming: return
        immediately after the upstream response starts; the lease is held until the stream ends or the client disconnects."""
        t0 = time.monotonic()
        stack = AsyncExitStack()
        try:
            url = await stack.enter_async_context(sup().lease(spec.name))
            headers = meta_headers(spec.name, time.monotonic() - t0)
            if not stream:
                r = await client().post(url + path, json=body)
                await stack.aclose()
                return Response(r.content, status_code=r.status_code, headers=headers,
                                media_type=r.headers.get("content-type", "application/json").split(";")[0])
            up = await client().send(client().build_request("POST", url + path, json=body), stream=True)
            stack.push_async_callback(up.aclose)
        except ApiError:
            await stack.aclose()
            raise
        except Exception as e:                                       # noqa: BLE001
            await stack.aclose()
            raise map_errors(e) from e

        async def gen():
            try:
                async for chunk in up.aiter_raw():
                    yield chunk
            finally:                                                  # runs on completion, error, or client disconnect
                await stack.aclose()

        return StreamingResponse(gen(), status_code=up.status_code, headers=headers,
                                 media_type=up.headers.get("content-type", "text/event-stream").split(";")[0])

    def handler(fn):
        async def wrapped(request: Request):
            try:
                return await fn(request)
            except ApiError as e:
                return error_response(e.status, e.kind, e.message, e.headers)
        return wrapped

    # ---- routes ----
    async def chat_completions(request: Request):
        body = await read_json(request)
        spec = resolve(body.get("model"), "llm")
        body = {**body, "model": spec.options.get("model", spec.name)}     # mlx_lm.server may try to load a different model id
        return await forward(spec, "/v1/chat/completions", body, stream=bool(body.get("stream")))

    async def decide(request: Request):
        body = await read_json(request)
        spec = resolve(body.pop("model", None), "decision")
        return await forward(spec, "/score_many" if "questions" in body else "/decide", body)

    async def entail(request: Request):
        body = await read_json(request)
        spec = resolve(body.pop("model", None), "nli")
        return await forward(spec, "/entail", body)

    async def models(request: Request):
        snap = {s["name"]: s for s in sup().snapshot()}
        return JSONResponse({"object": "list", "data": [
            {"id": s.name, "object": "model", "owned_by": "local", "aliases": list(s.aliases),
             "state": snap[s.name]["state"], "ready": snap[s.name]["state"] == "ready"}
            for s in catalog.backends.values() if s.kind == "llm"]})

    async def backends(request: Request):
        mem = sup().memory_status()
        return JSONResponse({"backends": sup().snapshot(), "gateway": {
            "port": catalog.gateway.port, "memory_budget_gb": mem["budget_gb"], "resident_est_gb": mem["resident_gb"],
            "free_est_gb": mem["free_gb"], "default_llm": catalog.gateway.default_llm,
            "system": await sup().system_status()}})

    async def healthz(request: Request):
        return JSONResponse({"status": "ok"})

    routes = [
        Route("/v1/chat/completions", handler(chat_completions), methods=["POST"]),
        Route("/v1/models", handler(models), methods=["GET"]),
        Route("/api/decide", handler(decide), methods=["POST"]),
        Route("/api/entail", handler(entail), methods=["POST"]),
        Route("/api/backends", handler(backends), methods=["GET"]),
        Route("/healthz", handler(healthz), methods=["GET"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    wrapped = LocalOnly(app, catalog.gateway.port)
    wrapped.starlette = app                                         # for tests / later route additions
    return wrapped
