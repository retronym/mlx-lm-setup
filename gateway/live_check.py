"""Exercise every MCP tool against a RUNNING gateway with the real models (uses the token in .gateway/token).

  .venv/bin/python -m gateway.live_check
Starts models on demand and stops what it started; takes about a minute on a warm machine.
"""
import asyncio, json, sys, time
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from . import auth, catalog as cat_mod


async def main() -> int:
    cat = cat_mod.load("gateway.toml")
    url = f"http://{cat.gateway.host}:{cat.gateway.port}/mcp"
    token = auth.load_or_create(cat.state_path() / "token")
    bad = 0

    async def call(s, tool, **args):
        t = time.time()
        res = await s.call_tool(tool, args)
        text = "".join(c.text for c in res.content if isinstance(c, TextContent))
        return res.isError, text, time.time() - t

    def show(title, err, text, secs, expect_error=False):
        nonlocal bad
        ok = err == expect_error
        bad += not ok
        body = text if len(text) < 230 else text[:230] + "..."
        print(f"[{'ok' if ok else 'FAIL'}] {title} ({secs:.1f}s)\n      {body}")

    async with streamablehttp_client(url) as (r, w, _):                                  # no token: inference + status only
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", [t.name for t in (await s.list_tools()).tools])
            show("backends_status", *await call(s, "backends_status"))
            show("start_backend WITHOUT token (must be refused)", *await call(s, "start_backend", name="jevstyle-2b"), expect_error=True)
            show("chat", *await call(s, "chat", message="Reply with exactly three words about the sea.", max_tokens=30, temperature=0.2))
            show("decide (single question)", *await call(s, "decide", state="I was charged twice and nobody answers my emails.",
                 question="Which team should handle this?", options={"billing": "payments and invoices", "technical": "bugs and outages", "sales": "new purchases"}, top_k=3))
            show("decide (several questions, one state)", *await call(s, "decide", state="Alice was beginning to get very tired of sitting by her sister.",
                 questions=[{"t": "choice", "ins": "What is the mood?", "crit": {"tired": None, "joyful": None, "angry": None}},
                            {"t": "noul", "ins": "The passage is set indoors."}]))
            show("entail", *await call(s, "entail", premise="def first_dup(xs):\n    seen=set()\n    for x in xs:\n        if x in seen: return x\n        seen.add(x)\n    return -1",
                 hypotheses=["first_dup returns -1 when there is no duplicate.", "first_dup returns None when there is no duplicate."]))
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            show("set_backend_policy (ttl 120)", *await call(s, "set_backend_policy", name="jevstyle-2b", ttl_s=120))
            show("stop_backend jevstyle-2b", *await call(s, "stop_backend", name="jevstyle-2b"))
            show("start_backend jevstyle-2b", *await call(s, "start_backend", name="jevstyle-2b"))
            show("backends_status (after)", *await call(s, "backends_status"))
            for n in ("qwen3-coder", "openjev-4b", "jevstyle-2b"):
                await call(s, "stop_backend", name=n)
            show("backends_status (everything stopped)", *await call(s, "backends_status"))
    print("\nALL OK" if not bad else f"\n{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
