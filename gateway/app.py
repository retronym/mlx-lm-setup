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
from .core import COLD_HEADER_THRESHOLD_S, ApiError, map_errors, run_embed, run_look, run_narrate, run_ask, run_interpret, run_search, run_translate, search_clusters, search_duplicates, search_get, search_links, search_outliers, search_stats, vision_models, vision_target, voices_listing, op_policy, op_start, op_stop, resolve as core_resolve, resolve_target as core_resolve_target, status_payload
from .mcp_server import build_mcp
from . import vision
from .supervisor import Supervisor

LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}
WEB = Path(__file__).parent / "web"
IMAGE_NAME = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.png$")
# The pages render model output, so lock them down: same-origin only, no framing, no sniffing. Inline script/style are
# allowed because each page is a single self-contained file; nothing is ever loaded from another origin.
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
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
            url = await stack.enter_async_context(sup().lease(spec.name, path))
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
    def has_images(messages) -> bool:
        return isinstance(messages, list) and any(isinstance(m, dict) and isinstance(m.get("content"), list) and
                                                  any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"]) for m in messages)

    async def vision_completion(spec, prof, body: dict) -> Response:
        """A vision backend answers whole (no token streaming); a client that asked for a stream gets it as one SSE chunk."""
        stream = bool(body.pop("stream", False))
        body.pop("stream_options", None)
        body = apply_defaults({**body, "messages": await asyncio.to_thread(vision.inline_messages, body.get("messages"))}, prof)
        resp = await forward(spec, "/v1/chat/completions", body)
        if not stream or resp.status_code != 200:
            return resp
        d = json.loads(resp.body)
        msg = d["choices"][0]["message"]
        chunk = {"id": d["id"], "object": "chat.completion.chunk", "created": d["created"], "model": d["model"],
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": msg.get("content", "")}, "finish_reason": d["choices"][0]["finish_reason"]}],
                 "usage": d.get("usage")}
        return Response(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", media_type="text/event-stream",
                        headers={k: v for k, v in resp.headers.items() if k.lower().startswith("x-gateway")})

    async def chat_completions(request: Request):
        body = await read_json(request)
        vt = vision_target(catalog, body.get("model"))
        if vt is not None:
            return await vision_completion(*vt, body)
        spec, prof = target(body.get("model"), "llm")
        if has_images(body.get("messages")):
            raise ApiError(400, "model_not_vision", f"{spec.name} does not take images; use a vision model: {', '.join(vision_models(catalog)) or 'none in the catalog'}")
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

    async def score(request: Request):
        """Continuation scoring: {"prompt", "candidates": [str], "model"?} -> {"logprobs": [...]}, log P(candidate | prompt)."""
        body = await read_json(request)
        spec, prof = target(body.pop("model", None), "score")
        return await forward(spec, "/score", apply_defaults(body, prof))

    async def speech(request: Request):
        """OpenAI-compatible text to speech: {"model"?, "input", "voice"?, "speed"?} -> audio/wav."""
        body = await read_json(request)
        spec, prof = target(body.pop("model", None), "tts")
        return await forward(spec, "/v1/audio/speech", apply_defaults(body, prof))

    async def speak(request: Request):
        """Like /v1/audio/speech but returns JSON: the clip's path on disk, duration and per-sentence-group timings."""
        body = await read_json(request)
        spec, prof = target(body.pop("model", None), "tts")
        return await forward(spec, "/speak", apply_defaults(body, prof))

    async def image(request: Request):
        """Text to image: {"prompt", "model"?, "seed"?, "width"?, "height"?, "steps"?, "name"?, "fresh"?}
        -> {path, name, width, height, seed, steps, model, gen_s, cached}; the picture itself is at GET /api/image/file/<name>."""
        body = await read_json(request)
        spec, prof = target(body.pop("model", None), "image")
        return await forward(spec, "/generate", apply_defaults(body, prof))

    async def image_models(request: Request):
        """Each image model with its description and defaults (the image page's model picker)."""
        return JSONResponse({"models": [{"model": s.name, "aliases": list(s.aliases), "description": s.description, "steps": s.options.get("steps"),
                                         "size": s.options.get("size", 1024), "est_mem_gb": s.est_mem_gb}
                                        for s in catalog.backends.values() if s.kind == "image"]})

    async def image_file(request: Request):
        name = request.path_params["name"]
        for s in catalog.backends.values():
            if s.kind == "image" and s.options.get("output_dir") and IMAGE_NAME.match(name) and (Path(s.options["output_dir"]) / name).is_file():
                return FileResponse(Path(s.options["output_dir"]) / name, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})
        return JSONResponse({"error": "not found"}, status_code=404)

    async def transcribe(request: Request):
        body = await read_json(request)
        spec, prof = target(body.pop("model", None), "stt")
        return await forward(spec, "/transcribe", apply_defaults(body, prof))

    async def voices(request: Request):
        """Each speech model with its preset voices and the named reference clips (chat site's voice picker, the voices tool)."""
        return JSONResponse({"models": voices_listing(catalog)})

    async def narrate(request: Request):
        """Timed narration: {"scenes": [{id, text with [[cue]] markers}], "model"?, "voice"?, "ref_audio"?, "speed"?, "fresh"?}
        -> per scene: clip path, duration, word timestamps, resolved cue times, and where the transcript differs from the script."""
        body = await read_json(request)
        try:
            speed = float(body.get("speed", 1.0))
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_arguments", "speed must be a number") from None
        return JSONResponse(await run_narrate(catalog, sup(), client(), body.get("scenes"), model=body.get("model"), voice=body.get("voice"),
                                              ref_audio=body.get("ref_audio"), speed=speed, fresh=bool(body.get("fresh"))))

    async def look(request: Request):
        """{"images": [path | data URI | "x.pdf#page=N" | directory], "prompt" | "preset", "context"?, "each"?, "gates"?, "model"?,
        "image_tokens"?, "max_tokens"?, "max_attempts"?} -> the answer (and parsed JSON), or per-image results when `each`."""
        body = await read_json(request)
        known = {"images", "prompt", "preset", "context", "system", "each", "gates", "image_tokens", "max_attempts", "model", "max_tokens", "temperature"}
        if set(body) - known:
            raise ApiError(400, "invalid_arguments", f"unknown keys {sorted(set(body) - known)}")
        if not isinstance(body.get("images"), list):
            raise ApiError(400, "invalid_arguments", "`images` must be a list")
        return JSONResponse(await run_look(catalog, sup(), client(), **body))

    async def translate(request: Request):
        """{"text"} | {"image": path | data URI}, "source"? (a hint; default: detect), "target"? (default English), "mode"? (auto | ocr |
        vision), "model"?, "max_attempts"? -> {source_language,
        translation, summary, markdown, route (text | ocr | vision), model, ...}. Screenshots go through macOS OCR first."""
        body = await read_json(request)
        known = {"text", "image", "source", "target", "mode", "model", "max_attempts"}
        if set(body) - known:
            raise ApiError(400, "invalid_arguments", f"unknown keys {sorted(set(body) - known)}")
        return JSONResponse(await run_translate(catalog, sup(), client(), **body))

    async def search(request: Request):
        """{"query", "k"? (1..50, default 20), "universe"? (default: the default universe), "projects"? [id], "sources"? [id | "project/source"],
        "kinds"? [file | issue | pr | comment | review | summary | commit | release | tag], "mode"? (hybrid | bm25 | vec), "rerank"? (default true), "open_only"? (hide closed issues and unmerged-closed PRs), "explain"? (add each hit's fusion and rerank arithmetic),
        "linked_to"? (only documents linked to this one: a hit `ref`, `scala/bug#123`, `#123`, a commit sha; a list means any of them) with "link_type"? [relation, e.g. "closed_by", "mentions"; name both ends, "closes" and "closed_by", for either direction],
        "has_link"? [relation, or "no_<relation>"] (e.g. ["closed_by"]: has a fix; ["no_closed_by"] with kinds ["issue"]: no fix), "refs_in_query"? (default true: a reference spelled out in the query, like `scala/bug#123`, puts that document and what links to it first), "related"? (false, or how many: documents linked to the top hits, shown apart as `related`; default from search.json), "link_boost"? (experiment: lift hits linked to the top ones, 0 = off),
        "since"?, "until"? (YYYY[-MM[-DD]], inclusive; files have no date and drop out), "date"? (created (default) | updated), "authors"? [GitHub login or git author name; any; "me" is search.json `me`],
        "sort"? (relevance (default) | recent: the best k newest first); an empty "query" lists what the filters match, newest first} ->
        {results: [{project, source, key, label, color, title, url, text, state?, bm25?, vec?, rerank?}], universe, missing, timing_ms, ...}."""
        body = await read_json(request)
        known = {"query", "k", "universe", "projects", "sources", "kinds", "mode", "rerank", "open_only", "explain", "text_chars", "model", "linked_to", "link_type", "has_link", "refs_in_query", "related", "link_boost",
                 "since", "until", "date", "authors", "sort"}
        if set(body) - known:
            raise ApiError(400, "invalid_arguments", f"unknown keys {sorted(set(body) - known)}")
        if not isinstance(body.get("query"), str):
            raise ApiError(400, "invalid_arguments", "`query` must be a string")
        return JSONResponse(await run_search(catalog, sup(), client(), **body))

    async def search_get_route(request: Request):
        """{"refs": [str] (the `ref` of search hits, 1..20), "scope"? (chunk | doc (default) | file), "max_chars"? (100..50000, default 6000), "offset"?, "lines"? ("120-180", scope file)}
        -> {results: [{ref, found, title, url, kind, state?, author?, text, total_chars, truncated, next_offset?, ...}]}. Reads files; starts nothing."""
        body = await read_json(request)
        known = {"refs", "scope", "max_chars", "offset", "lines", "model"}
        if set(body) - known:
            raise ApiError(400, "invalid_arguments", f"unknown keys {sorted(set(body) - known)}")
        if not isinstance(body.get("refs"), list):
            raise ApiError(400, "invalid_arguments", "`refs` must be a list of strings")
        return JSONResponse(search_get(catalog, **body))

    async def search_ask_route(request: Request):
        """{"question" (3..1000 characters), "universe"?, "k"? (1..20, default 5), "rounds"? (1..3, default 2), "trace"? (default true)} -> {plan, answers: [{ref, node, title, url,
        kind, state, verdict (yes: the LLM picked it; no: shown, not picked; null: not shown), quote?, reason?, via?, p, routes, line}], trace, seconds, timing}.
        The agentic query layer (ASK.md): plans, searches several routes, follows issues to the PRs that close them, and lets the LLM pick. 10-30 s."""
        body = await read_json(request)
        known = {"question", "universe", "k", "rounds", "trace", "model", "plan"}
        if set(body) - known:
            raise ApiError(400, "invalid_arguments", f"unknown keys {sorted(set(body) - known)}")
        return JSONResponse(await run_ask(catalog, **body))

    async def search_interpret_route(request: Request):
        """{"question"} -> {plan: {intent (one | list | topic), criterion, kinds, queries, since?, until?, authors?, open_only, notes}, seconds}: the planner
        alone, about 1.5 s with the LLM loaded. Pass the plan (edited or not) to /api/search/ask as "plan" to skip planning again."""
        body = await read_json(request)
        if set(body) - {"question", "model"}:
            raise ApiError(400, "invalid_arguments", f"unknown keys {sorted(set(body) - {'question', 'model'})}")
        return JSONResponse(await run_interpret(catalog, **body))

    async def search_links_route(request: Request):
        """{"ref" (a hit `ref`, `scala/bug#123`, `#123`, `SI-123`, a commit sha), "types"? [relation], "limit"? (1..200, default 30), "story"? (time-ordered documents around it), "universe"?}
        -> {found, node, counts, links: [{rel, id, kind, title, state, created, url, get_ref, conf, how, snip}]} or {story: [...]}. Reads a file; starts nothing."""
        body = await read_json(request)
        known = {"ref", "types", "limit", "story", "universe", "model"}
        if set(body) - known:
            raise ApiError(400, "invalid_arguments", f"unknown keys {sorted(set(body) - known)}")
        if not isinstance(body.get("ref"), str):
            raise ApiError(400, "invalid_arguments", "`ref` must be a string")
        return JSONResponse(search_links(catalog, **body))

    async def search_status(request: Request):
        """Universes with their projects and sources (labels, colours, priorities) and what each index holds; reads files, starts nothing.
        ?universe=<id> picks the universe whose counts are returned (default: the default one)."""
        q = request.query_params
        return JSONResponse(search_stats(catalog, q.get("model"), q.get("universe")))

    def neighbour_filters(request: Request, allowed: dict) -> dict:
        """The shared query-string filters of the duplicates and clusters views; `allowed` maps a name to its parser."""
        q, out = request.query_params, {}
        for name, parse in allowed.items():
            vals = q.getlist(name)
            if not vals:
                continue
            try:
                out[name] = [v for v in vals if v] if name == "projects" else parse(vals[-1])
            except ValueError:
                raise ApiError(400, "invalid_arguments", f"`{name}` is invalid: {vals[-1]!r}") from None
        return out

    def flag(v: str) -> bool:
        if v not in ("0", "1", "true", "false"):
            raise ValueError(v)
        return v in ("1", "true")

    common = {"state": str, "kind": str, "since": str, "until": str, "projects": str}
    paging = {"limit": lambda v: max(1, min(int(v), 500)), "offset": lambda v: max(0, int(v))}

    async def search_duplicates_view(request: Request):
        """Duplicate candidates: close issue / PR pairs, best first, from the neighbours database the refresh builds. Query: universe, min_sim
        (default 0.9), state (any | open: one of the pair is open | closed), kind (issue | pr | mixed | any), since / until (YYYY[-MM[-DD]]; a pair is
        kept if EITHER item is in range), projects (repeatable; EITHER item), adjacent (default 1: drop same-repo pairs with numbers that close when one
        is closed, an old import's copies; 0 keeps them), templated (0 | 1), limit, offset."""
        f = neighbour_filters(request, {**common, "min_sim": float, "adjacent": lambda v: max(0, int(v)), "templated": flag, **paging})
        if "kind" not in f:
            f["kind"] = "issue"
        return JSONResponse(search_duplicates(catalog, request.query_params.get("model"), request.query_params.get("universe"), **f))

    async def search_clusters_view(request: Request):
        """Topic clusters with counts under the filters (universe, state, kind issue | pr | any, since, until, projects). With cluster=<k>, that
        cluster's items (limit, offset) instead."""
        f = neighbour_filters(request, {**common, "cluster": int, **paging})
        return JSONResponse(search_clusters(catalog, request.query_params.get("model"), request.query_params.get("universe"), **f))

    async def search_outliers_view(request: Request):
        """Issues and PRs far from everything else, most outlying first. Query: universe, by (iso: similarity to the nearest other item | ctr: similarity
        to the centre of its cluster; default iso), state, kind (issue | pr | any), since, until, projects, templated (0 | 1: keep dependency bumps and
        release procedures), limit, offset."""
        f = neighbour_filters(request, {**common, "by": str, "templated": flag, **paging})
        return JSONResponse(search_outliers(catalog, request.query_params.get("model"), request.query_params.get("universe"), **f))

    async def embeddings(request: Request):
        """OpenAI-compatible embeddings: {"input": str | [str], "model"?} -> {data: [{embedding, index}], model, usage}. Documents are
        embedded as-is; add "kind": "query" for the retrieval-instruction form used for search queries."""
        body = await read_json(request)
        out = await run_embed(catalog, sup(), client(), {k: v for k, v in body.items() if k in ("input", "kind")}, body.get("model"))
        return JSONResponse({"object": "list", "model": out["model"], "data": [{"object": "embedding", "index": i, "embedding": e}
                                                                              for i, e in enumerate(out["embeddings"])],
                             "usage": {"prompt_tokens": 0, "total_tokens": 0}})

    async def rerank(request: Request):
        """{"query", "documents": [str], "model"?} -> {scores: [P(document answers the query)], model}."""
        body = await read_json(request)
        spec, prof = target(body.pop("model", None), "search")
        return await forward(spec, "/rerank", apply_defaults(body, prof))

    async def look_expand(request: Request):
        """{"images": [...]} -> the same list with directories replaced by their image files, so a client can send one request per
        image and show progress. Nothing is read beyond the directory listing."""
        body = await read_json(request)
        if not isinstance(body.get("images"), list) or not all(isinstance(i, str) for i in body["images"]):
            raise ApiError(400, "invalid_arguments", "`images` must be a list of strings")
        out = await asyncio.to_thread(vision.expand, body["images"])
        if len(out) > vision.MAX_EACH:
            raise ApiError(400, "invalid_arguments", f"too many images ({len(out)}; at most {vision.MAX_EACH})")
        return JSONResponse({"images": out})

    async def vision_presets(request: Request):
        return JSONResponse({"models": [{"name": s.name, "description": s.description, "image_tokens": s.options.get("image_tokens")}
                                        for s in catalog.backends.values() if s.kind == "vision"],
                             "presets": {k: {kk: v[kk] for kk in ("title", "prompt", "gates", "image_tokens", "each")} for k, v in vision.PRESETS.items()}})

    async def models(request: Request):
        snap = {s["name"]: s for s in sup().snapshot()}
        return JSONResponse({"object": "list", "data": [
            {"id": s.name, "object": "model", "owned_by": "local", "aliases": list(s.aliases),
             "state": snap[s.name]["state"], "ready": snap[s.name]["state"] == "ready", **({"vision": True} if s.kind == "vision" else {})}
            for s in catalog.backends.values() if s.kind in ("llm", "vision")] + [
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

    async def requests_timeline(request: Request):
        """Read-only: requests in flight and recently finished, per backend, with lifecycle events (the home page's timeline)."""
        try:
            window = float(request.query_params.get("window_s", 900))
        except ValueError:
            raise ApiError(400, "invalid_parameter", "window_s must be a number") from None
        return JSONResponse(sup().requests_snapshot(min(max(window, 10.0), 3600.0)))

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
        Route("/v1/audio/speech", handler(speech), methods=["POST"]),
        Route("/v1/models", handler(models), methods=["GET"]),
        Route("/api/speak", handler(speak), methods=["POST"]),
        Route("/api/image", handler(image), methods=["POST"]),
        Route("/api/image/models", handler(image_models), methods=["GET"]),
        Route("/api/image/file/{name}", image_file, methods=["GET"]),
        Route("/api/transcribe", handler(transcribe), methods=["POST"]),
        Route("/api/voices", handler(voices), methods=["GET"]),
        Route("/api/narrate", handler(narrate), methods=["POST"]),
        Route("/api/look", handler(look), methods=["POST"]),
        Route("/api/translate", handler(translate), methods=["POST"]),
        Route("/api/search", handler(search), methods=["POST"]),
        Route("/api/search/status", handler(search_status), methods=["GET"]),
        Route("/api/search/get", handler(search_get_route), methods=["POST"]),
        Route("/api/search/links", handler(search_links_route), methods=["POST"]),
        Route("/api/search/ask", handler(search_ask_route), methods=["POST"]),
        Route("/api/search/interpret", handler(search_interpret_route), methods=["POST"]),
        Route("/api/search/duplicates", handler(search_duplicates_view), methods=["GET"]),
        Route("/api/search/clusters", handler(search_clusters_view), methods=["GET"]),
        Route("/api/search/outliers", handler(search_outliers_view), methods=["GET"]),
        Route("/api/rerank", handler(rerank), methods=["POST"]),
        Route("/v1/embeddings", handler(embeddings), methods=["POST"]),
        Route("/api/look/expand", handler(look_expand), methods=["POST"]),
        Route("/api/vision/presets", handler(vision_presets), methods=["GET"]),
        Route("/api/decide", handler(decide), methods=["POST"]),
        Route("/api/entail", handler(entail), methods=["POST"]),
        Route("/api/score", handler(score), methods=["POST"]),
        Route("/api/backends", handler(backends), methods=["GET"]),
        Route("/api/requests", handler(requests_timeline), methods=["GET"]),
        Route("/api/models", handler(models_rich), methods=["GET"]),
        Route("/api/models/discovered", handler(models_discovered), methods=["GET"]),
        Route("/api/models/snippet", handler(models_snippet), methods=["GET"]),
        Route("/api/backends/{name}/start", handler(api_start), methods=["POST"]),
        Route("/api/backends/{name}/stop", handler(api_stop), methods=["POST"]),
        Route("/api/backends/{name}/policy", handler(api_policy), methods=["POST"]),
        Route("/api/events", events, methods=["GET"]),
        Route("/", page("home.html"), methods=["GET"]),
        Route("/chat", page("chat.html"), methods=["GET"]),
        Route("/jev", page("jev.html"), methods=["GET"]),
        Route("/speech", page("speech.html"), methods=["GET"]),
        Route("/image", page("image.html"), methods=["GET"]),
        Route("/vision", page("vision.html"), methods=["GET"]),
        Route("/translate", page("translate.html"), methods=["GET"]),
        Route("/search", page("search.html"), methods=["GET"]),
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
