"""The gateway's HTTP front door (Starlette): OpenAI-compatible chat, typed decisions, NLI, read-only status.

Every proxied request goes through ``Supervisor.lease`` so the backend is started lazily, requests queue per backend, and a
backend is never passivated while a request (including a streaming response) is in flight.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route

from . import auth
from .catalog import Catalog
from . import discovery, modelinfo
from .profiles import apply_defaults
from .core import COLD_HEADER_THRESHOLD_S, ApiError, map_errors, op_policy, op_start, op_stop, resolve as core_resolve, resolve_target as core_resolve_target, status_payload
from .mcp_server import build_mcp
from .supervisor import Supervisor

LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}
WEB = Path(__file__).parent / "web"
# The pages render model output, so lock them down: same-origin only, no framing, no sniffing. Inline script/style are
# allowed because each page is a single self-contained file; nothing is ever loaded from another origin.
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
}


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
def create_app(catalog: Catalog, supervisor: Supervisor | None = None, *, read_timeout_s: float = 600.0, token: str | None = None) -> Starlette:
    token = token or auth.load_or_create((supervisor.state_dir if supervisor else catalog.state_path()) / "token")
    state: dict = {"sup": supervisor, "client": None, "owns_sup": supervisor is None}
    stopping = asyncio.Event()                                       # set when shutdown begins: SSE streams end themselves

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
            async with mcp.session_manager.run():                    # MCP streamable-HTTP sessions live as long as the app
                yield
        finally:
            if state["owns_sup"]:
                await sup().shutdown()
            if state["client"] is not None:
                await state["client"].aclose()
                state["client"] = None

    def resolve(model: str | None, kind: str):
        return core_resolve(catalog, model, kind)

    def target(model: str | None, kind: str):
        return core_resolve_target(catalog, model, kind)       # (backend, profile or None)

    async def read_json(request: Request) -> dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "invalid_json", "request body is not valid JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")
        return body

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
        spec, prof = target(body.get("model"), "llm")
        body = apply_defaults(body, prof)                                  # profile defaults fill only what the client omitted
        body = {**body, "model": spec.options.get("model", spec.name)}     # mlx_lm.server may try to load a different model id
        return await forward(spec, "/v1/chat/completions", body, stream=bool(body.get("stream")))

    async def decide(request: Request):
        body = await read_json(request)
        spec, prof = target(body.pop("model", None), "decision")
        body = apply_defaults(body, prof)
        return await forward(spec, "/score_many" if "questions" in body else "/decide", body)

    async def entail(request: Request):
        body = await read_json(request)
        spec, prof = target(body.pop("model", None), "nli")
        body = apply_defaults(body, prof)
        return await forward(spec, "/entail", body)

    async def models(request: Request):
        snap = {s["name"]: s for s in sup().snapshot()}
        return JSONResponse({"object": "list", "data": [
            {"id": s.name, "object": "model", "owned_by": "local", "aliases": list(s.aliases),
             "state": snap[s.name]["state"], "ready": snap[s.name]["state"] == "ready"}
            for s in catalog.backends.values() if s.kind == "llm"] + [
            {"id": p.name, "object": "model", "owned_by": "local", "profile_of": p.backend, "description": p.description,
             "aliases": list(p.aliases), "state": snap[p.backend]["state"], "ready": snap[p.backend]["state"] == "ready"}
            for p in catalog.profiles.values() if catalog.backends[p.backend].kind == "llm"]})

    def discovery_params(request: Request) -> tuple[int, int | None]:
        try:
            ctx = int(request.query_params.get("context_tokens", 32768))
            bits = request.query_params.get("kv_bits")
            bits = int(bits) if bits else None
        except ValueError:
            raise ApiError(400, "invalid_parameter", "context_tokens and kv_bits must be integers") from None
        if not 256 <= ctx <= 4_000_000:
            raise ApiError(400, "invalid_parameter", "context_tokens must be between 256 and 4000000")
        if bits not in (None, 2, 3, 4, 5, 6, 8):
            raise ApiError(400, "invalid_parameter", "kv_bits must be one of 2, 3, 4, 5, 6, 8")
        return ctx, bits

    disc_cache: dict = {"t": 0.0, "by_model": {}}

    async def discovered_by_model() -> dict:
        """Discovery results by model id, refreshed at most once a minute (the picker polls every few seconds)."""
        if time.monotonic() - disc_cache["t"] > 60:
            found = await asyncio.to_thread(discovery.scan, catalog, None, llm_only=True)
            disc_cache.update(t=time.monotonic(), by_model={f.id: f for f in found})
        return disc_cache["by_model"]

    async def models_rich(request: Request):
        """For the chat site's model picker: each LLM and profile with description, memory breakdown, thinking default,
        context and KV cost, profile defaults, and live state. Read-only."""
        snap = {s["name"]: s for s in sup().snapshot()}
        try:
            by = await discovered_by_model()
        except Exception:                                            # noqa: BLE001  discovery is a nicety; never break the picker
            by = {}
        return JSONResponse({"models": modelinfo.build_entries(catalog, snap, by)})

    async def models_discovered(request: Request):
        """Read-only: MLX LLMs found on disk (HF cache, LM Studio) with sizes, KV estimates and catalog status."""
        ctx, bits = discovery_params(request)
        found = await asyncio.to_thread(discovery.scan, catalog, None, llm_only=True)       # stats + one subprocess: off the event loop
        return JSONResponse({"context_tokens": ctx, "kv_bits": bits, "models": [discovery.to_dict(f, ctx, bits) for f in found]})

    async def models_snippet(request: Request):
        """Read-only: a [backends.*] table for one discovered model, as text. Takes a model id, never a path."""
        ctx, bits = discovery_params(request)
        mid = request.query_params.get("id", "")
        found = await asyncio.to_thread(discovery.scan, catalog, None, llm_only=True)
        hit = next((f for f in found if f.id == mid), None)
        if hit is None:
            raise ApiError(404, "model_not_found", f"{mid!r} is not among the discovered models")
        return PlainTextResponse(discovery.snippet(hit, context_tokens=ctx, kv_bits=bits, python=discovery.catalog_python(catalog) or "python3"))

    async def backends(request: Request):
        return JSONResponse(await status_payload(catalog, sup()))

    def require_token(request: Request) -> None:
        if not auth.valid(token, request.headers.get("authorization")):
            raise ApiError(401, "unauthorized", "this endpoint changes what is running and needs the gateway token "
                                                "(Authorization: Bearer <token>; see .gateway/token)", {"WWW-Authenticate": "Bearer"})

    async def api_start(request: Request):
        require_token(request)
        return JSONResponse(await op_start(sup(), request.path_params["name"]))

    async def api_stop(request: Request):
        require_token(request)
        body = await read_json(request) if await request.body() else {}
        return JSONResponse(await op_stop(sup(), request.path_params["name"], bool(body.get("force", False))))

    async def api_policy(request: Request):
        require_token(request)
        body = await read_json(request)
        return JSONResponse(op_policy(catalog, sup(), request.path_params["name"], body.get("ttl_s"), body.get("pinned")))

    def sse(event: str, data) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    async def events(request: Request):
        """Server-sent events for the admin site: a status snapshot every second (so idle countdowns tick) and every lifecycle event."""
        q = sup().subscribe()

        async def gen():
            try:
                yield sse("snapshot", await status_payload(catalog, sup()))
                for e in list(sup().events)[-60:]:
                    yield sse("log", {**e, "replay": True})
                while not stopping.is_set():
                    try:
                        e = await asyncio.wait_for(q.get(), timeout=1.0)
                        yield sse("log", e)
                    except asyncio.TimeoutError:
                        pass
                    yield sse("snapshot", await status_payload(catalog, sup()))
            finally:
                sup().unsubscribe(q)                                  # also runs when the client disconnects

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def page(name: str):
        async def serve(request: Request):
            return FileResponse(WEB / name, media_type="text/html", headers=PAGE_HEADERS)
        return serve

    VENDOR = {"marked.min.js", "purify.min.js"}               # fixed allow-list: no path traversal, same-origin scripts only

    async def vendor(request: Request):
        name = request.path_params["name"]
        if name not in VENDOR:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(WEB / "vendor" / name, media_type="text/javascript", headers={"X-Content-Type-Options": "nosniff"})

    async def healthz(request: Request):
        return JSONResponse({"status": "ok"})

    routes = [
        Route("/v1/chat/completions", handler(chat_completions), methods=["POST"]),
        Route("/v1/models", handler(models), methods=["GET"]),
        Route("/api/decide", handler(decide), methods=["POST"]),
        Route("/api/entail", handler(entail), methods=["POST"]),
        Route("/api/backends", handler(backends), methods=["GET"]),
        Route("/api/models", handler(models_rich), methods=["GET"]),
        Route("/api/models/discovered", handler(models_discovered), methods=["GET"]),
        Route("/api/models/snippet", handler(models_snippet), methods=["GET"]),
        Route("/api/backends/{name}/start", handler(api_start), methods=["POST"]),
        Route("/api/backends/{name}/stop", handler(api_stop), methods=["POST"]),
        Route("/api/backends/{name}/policy", handler(api_policy), methods=["POST"]),
        Route("/api/events", events, methods=["GET"]),
        Route("/", page("chat.html"), methods=["GET"]),
        Route("/chat", page("chat.html"), methods=["GET"]),
        Route("/jev", page("jev.html"), methods=["GET"]),
        Route("/admin", page("admin.html"), methods=["GET"]),
        Route("/vendor/{name}", vendor, methods=["GET"]),
        Route("/healthz", handler(healthz), methods=["GET"]),
    ]
    mcp = build_mcp(catalog, sup, client, token)
    routes.append(Mount("/", app=mcp.streamable_http_app()))       # /mcp, after our own routes
    app = Starlette(routes=routes, lifespan=lifespan)
    wrapped = LocalOnly(app, catalog.gateway.port)
    wrapped.starlette = app                                         # for tests / later route additions
    wrapped.stopping = stopping                                     # the server sets this on SIGTERM/SIGINT
    return wrapped
