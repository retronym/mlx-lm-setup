"""MCP tools for the gateway (streamable HTTP, stateless). Built on the same lease/queue/passivation path as the HTTP routes.

Inference tools (chat, decide, entail) and read-only status are open on localhost. Tools that change what is running
(start_backend, stop_backend, set_backend_policy) require the gateway token in the request's Authorization header, so a
compromised prompt cannot make the model fleet start or stop on its own.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from . import auth
from .catalog import Catalog
from .core import ApiError, map_errors, post_json, resolve
from .supervisor import GatewayError, Supervisor

INSTRUCTIONS = """Local model gateway (Apple silicon, localhost). Models start on first use and stop when idle.
- chat: a local LLM (default Qwen3-Coder). Good for bounded, checkable work: summaries, extraction, boilerplate, simple
  refactors. It can be wrong or hallucinate, so verify; keep design, review and judgment calls for yourself.
- decide: typed decisions (choose among options, rate on a scale, yes/no) with probabilities, from a local decision model;
  nothing is generated. Good for classification, routing, triage, tagging.
- entail: check claims against a source text with a local NLI model (entailment / contradiction / neutral). A weak signal.
- backends_status: what is running, memory use and idle timers. start_backend / stop_backend / set_backend_policy change
  what is running and need the gateway token.
A call that needs a model that is not running waits for it to start (seconds; the first call after idle is slower)."""


def _compact_backend(s: dict) -> dict:
    keep = ("name", "kind", "state", "busy", "est_mem_gb", "holds_memory", "ttl_s", "ttl_left_s", "idle_s", "pinned",
            "requests", "starts", "passivations", "last_start_s", "last_error")
    return {k: s[k] for k in keep if k in s}


def build_mcp(catalog: Catalog, get_supervisor: Callable[[], Supervisor], get_client: Callable[[], httpx.AsyncClient], token: str) -> FastMCP:
    mcp = FastMCP("local-models", instructions=INSTRUCTIONS, host=catalog.gateway.host, port=catalog.gateway.port,
                  stateless_http=True, json_response=True)

    def fail(e: Exception) -> ToolError:
        err = map_errors(e)
        return ToolError(f"{err.kind}: {err.message}")

    def authorize(ctx: Context) -> None:
        req = ctx.request_context.request
        header = req.headers.get("authorization") if req is not None else None
        if not auth.valid(token, header):
            raise ToolError("unauthorized: this tool changes what is running and needs the gateway token. Re-register the MCP "
                            "server with --header \"Authorization: Bearer <token>\" (the token is in .gateway/token; see README).")

    def spec_for(model: str | None, kind: str):
        try:
            return resolve(catalog, model, kind)
        except ApiError as e:
            raise fail(e) from None

    async def call(spec, path: str, body: dict) -> tuple[Any, dict]:
        try:
            r, meta = await post_json(get_supervisor(), get_client(), spec, path, body)
        except ApiError as e:
            raise fail(e) from None
        if r.status_code != 200:
            raise ToolError(f"backend_error: {spec.name} returned {r.status_code}: {r.text[:300]}")
        return r.json(), meta

    # ---- read-only ---------------------------------------------------------------------------------------------------
    @mcp.tool()
    async def backends_status() -> dict:
        """Which local models are running, with state, memory estimate, idle timer, TTL and pin; plus the memory budget and
        the system's free memory and swap. Does not start anything."""
        sup = get_supervisor()
        return {"backends": [_compact_backend(s) for s in sup.snapshot()], "memory": sup.memory_status(),
                "system": await sup.system_status(), "default_llm": catalog.gateway.default_llm}

    # ---- inference ---------------------------------------------------------------------------------------------------
    @mcp.tool()
    async def chat(message: str | None = None, messages: list[dict[str, str]] | None = None, model: str | None = None,
                   system: str | None = None, max_tokens: int = 1024, temperature: float = 0.7) -> dict:
        """Ask a local LLM. Pass either `message` (a single user prompt) or `messages` (a list of {role, content}); optional
        `system` prompt. Returns the reply text with token usage and timing. Starts the model if needed (cold start: seconds)."""
        if (message is None) == (messages is None):
            raise ToolError("invalid_arguments: pass exactly one of `message` or `messages`")
        msgs = [{"role": "user", "content": message}] if message is not None else list(messages or [])
        if not msgs:
            raise ToolError("invalid_arguments: `messages` is empty")
        if system:
            msgs = [{"role": "system", "content": system}, *msgs]
        spec = spec_for(model, "llm")
        t0 = time.monotonic()
        data, meta = await call(spec, "/v1/chat/completions", {
            "model": spec.options.get("model", spec.name), "messages": msgs, "max_tokens": max_tokens,
            "temperature": temperature, "stream": False})
        choice = (data.get("choices") or [{}])[0]
        return {"text": (choice.get("message") or {}).get("content", ""), "model": meta["backend"],
                "finish_reason": choice.get("finish_reason"), "usage": data.get("usage"),
                "secs": round(time.monotonic() - t0, 2), "cold_start_s": meta["cold_start_s"]}

    @mcp.tool()
    async def decide(state: str, question: str | None = None, options: list[str] | dict[str, str | None] | None = None,
                     qtype: str | None = None, questions: list[dict] | None = None, model: str | None = None, top_k: int = 5) -> dict:
        """Typed decisions about `state` (any text or JSON) from a local decision model, with probabilities; nothing is generated.
        One question: `question` plus `options` (a list of names, or {name: description}); qtype is "choice" (default when
        options are given), "score" (options = 2..10 ordered level descriptions) or "noul" (yes/no statement, no options).
        Several questions about the same state: `questions` = [{"t": "choice"|"score"|"noul", "ins": question, "crit": options}].
        Returns, per question, the answer and the `top_k` most likely options (0 = all)."""
        if questions is not None and question is not None:
            raise ToolError("invalid_arguments: pass either `question` or `questions`, not both")
        if questions is None and question is None:
            raise ToolError("invalid_arguments: pass `question` (with `options`) or `questions`")
        spec = spec_for(model, "decision")
        if questions is not None:
            data, meta = await call(spec, "/score_many", {"state": state, "questions": questions})
            results = data
        else:
            body: dict = {"state": state, "question": question}
            if options is not None:
                body["options"] = options
            if qtype is not None:
                body["qtype"] = qtype
            data, meta = await call(spec, "/decide", body)
            results = [data]
        out = []
        for r in results:
            probs = sorted((r.get("probabilities") or {}).items(), key=lambda kv: -kv[1])
            out.append({"answer": r.get("answer"), "top_probability": r.get("top_probability"),
                        "top": [[k, round(v, 4)] for k, v in (probs if top_k <= 0 else probs[:top_k])]})
        return {"model": meta["backend"], "results": out, "cold_start_s": meta["cold_start_s"]}

    @mcp.tool()
    async def entail(premise: str, hypotheses: list[str], model: str | None = None) -> dict:
        """Natural-language inference with a local NLI model: for each hypothesis, the probability that the `premise` entails
        it, contradicts it, or is neutral to it. Use it to check claims against source text (summaries, code descriptions).
        A weak signal for claims needing domain knowledge, and not hardened against prompt injection in the premise."""
        if not hypotheses:
            raise ToolError("invalid_arguments: `hypotheses` is empty")
        spec = spec_for(model, "nli")
        data, meta = await call(spec, "/entail", {"premise": premise, "hypotheses": hypotheses})
        labels = data["labels"]
        res = []
        for h, p in zip(hypotheses, data["probs"]):
            probs = {lab: round(x, 4) for lab, x in zip(labels, p)}
            res.append({"hypothesis": h, "label": max(probs, key=probs.get), **probs})
        return {"model": meta["backend"], "results": res, "cold_start_s": meta["cold_start_s"]}

    # ---- lifecycle (token required) -----------------------------------------------------------------------------------
    @mcp.tool()
    async def start_backend(name: str, ctx: Context) -> dict:
        """Start a local model now (idempotent) so the next call is fast. Needs the gateway token. May evict idle models to
        make room within the memory budget."""
        authorize(ctx)
        sup = get_supervisor()
        try:
            b = await sup.start(name)
        except GatewayError as e:
            raise fail(e) from None
        return {"name": name, "state": b.state.value, "start_s": b.start_secs, "est_mem_gb": b.spec.est_mem_gb}

    @mcp.tool()
    async def stop_backend(name: str, ctx: Context, force: bool = False) -> dict:
        """Stop a local model now (idempotent) to free its memory. Waits for in-flight requests unless `force`. Needs the gateway token."""
        authorize(ctx)
        sup = get_supervisor()
        try:
            await sup.stop(name, force=force)
        except GatewayError as e:
            raise fail(e) from None
        return _compact_backend(next(s for s in sup.snapshot() if s["name"] == name))

    @mcp.tool()
    async def set_backend_policy(name: str, ctx: Context, ttl_s: float | None = None, pinned: bool | None = None) -> dict:
        """Change a model's idle timeout (`ttl_s` seconds, 0 = never passivate) and/or `pinned` (never evicted or passivated).
        Takes effect immediately, lasts until the gateway restarts. Needs the gateway token."""
        authorize(ctx)
        sup = get_supervisor()
        try:
            b = sup._get(name)
        except GatewayError as e:
            raise fail(e) from None
        if pinned and not b.pinned:
            total = sum(r.spec.est_mem_gb for r in sup.rt.values() if r.pinned or r is b)
            if total > catalog.gateway.memory_budget_gb:
                raise ToolError(f"invalid_arguments: pinning {name} would pin {total:g} GB, more than the {catalog.gateway.memory_budget_gb:g} GB budget")
        try:
            sup.set_policy(name, ttl_s=ttl_s, pinned=pinned)
        except ValueError as e:
            raise ToolError(f"invalid_arguments: {e}") from None
        return _compact_backend(next(s for s in sup.snapshot() if s["name"] == name))

    return mcp
