"""MCP tools over real streamable HTTP, with the SDK's own client against a real gateway (fake backends)."""
import asyncio, contextlib, json, socket, tempfile, unittest
from pathlib import Path

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from gateway.app import create_app
from gateway.supervisor import State, Supervisor
from gateway.tests.test_supervisor import catalog, until

TOKEN = "test-token-123"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.cat = catalog({
            "llm": {"kind": "llm", "est_mem_gb": 6, "flags": ["--ready-delay", "0.3"]},
            "llm2": {"kind": "llm", "est_mem_gb": 6},
            "dec": {"kind": "decision", "est_mem_gb": 1},
            "nli": {"kind": "nli", "est_mem_gb": 1},
            "voice": {"kind": "tts", "est_mem_gb": 1},
            "ears": {"kind": "stt", "est_mem_gb": 1},
            "eyes": {"kind": "vision", "est_mem_gb": 1},
            "find": {"kind": "search", "est_mem_gb": 1},
        }, port=self.port, default_llm="llm", memory_budget_gb=10, room_timeout_s=0.4)
        self.sup = Supervisor(self.cat, state_dir=Path(self.tmp.name) / "state", reap_interval=0.05, health_interval=0.05, grace_s=0.5)
        app = create_app(self.cat, supervisor=self.sup, token=TOKEN)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"))
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    async def asyncTearDown(self):
        self.server.should_exit = True
        await self.task
        await self.sup.shutdown()
        self.tmp.cleanup()

    @contextlib.asynccontextmanager
    async def client(self, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else None
        async with streamablehttp_client(self.url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s

    async def call(self, session, tool, **args):
        res = await session.call_tool(tool, args)
        text = "".join(c.text for c in res.content if isinstance(c, TextContent))
        if res.structuredContent is not None:
            return res.isError, res.structuredContent, text
        try:
            return res.isError, (None if res.isError else json.loads(text)), text
        except json.JSONDecodeError:
            return res.isError, text, text

    # ------------------------------------------------------------------------------------------------------------
    async def test_tools_are_listed_with_descriptions_and_instructions(self):
        async with self.client() as s:
            tools = {t.name: t for t in (await s.list_tools()).tools}
            self.assertEqual(set(tools), {"backends_status", "chat", "iterate", "decide", "entail", "speak", "generate_image", "transcribe", "narrate", "voices", "look", "translate", "search", "ask", "get", "links", "search_universes", "start_backend", "stop_backend", "set_backend_policy"})
            self.assertTrue(all(t.description for t in tools.values()))
            self.assertIn("token", tools["start_backend"].description)
            init = await s.initialize()
            self.assertIn("Local model gateway", init.instructions)

    async def test_status_is_open_and_does_not_start_anything(self):
        async with self.client() as s:
            err, data, _ = await self.call(s, "backends_status")
            self.assertFalse(err)
            self.assertEqual([b["name"] for b in data["backends"]], ["llm", "llm2", "dec", "nli", "voice", "ears", "eyes", "find"])
            self.assertEqual(data["memory"]["budget_gb"], 10)
            self.assertEqual(data["default_llm"], "llm")
            self.assertIn("free_pct", data["system"])
        self.assertEqual(self.sup.rt["llm"].starts, 0)

    async def test_chat(self):
        async with self.client() as s:
            err, data, _ = await self.call(s, "chat", message="hello", system="be brief")
            self.assertFalse(err)
            self.assertEqual((data["text"], data["model"]), ("echo:2", "llm"))        # system + user message reached the backend
            self.assertGreater(data["cold_start_s"], 0.2)                              # lazily started
            err, data, _ = await self.call(s, "chat", messages=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}])
            self.assertEqual((data["text"], data["cold_start_s"]), ("echo:3", 0.0))     # warm now
            for args in ({}, {"message": "x", "messages": [{"role": "user", "content": "y"}]}, {"messages": []}):
                err, _, text = await self.call(s, "chat", **args)
                self.assertTrue(err)
                self.assertIn("invalid_arguments", text)
            err, _, text = await self.call(s, "chat", message="x", model="dec")        # wrong kind
            self.assertTrue(err)
            self.assertIn("wrong_model_kind", text)
            err, _, text = await self.call(s, "chat", message="x", model="nope")
            self.assertIn("model_not_found", text)

    async def test_look_through_mcp(self):
        png = Path(self.tmp.name) / "still.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
        async with self.client() as s:
            err, data, text = await self.call(s, "look", images=[str(png)], preset="layout")
            self.assertFalse(err, text)
            self.assertEqual((data["flagged"], data["results"][0]["json"]["ok"], data["model"]), ([], True, "eyes"))
            err, _, text = await self.call(s, "look", images=["https://example.com/x.png"], prompt="hi")
            self.assertTrue(err)
            self.assertIn("not fetched", text)

    async def test_iterate_through_mcp(self):
        async with self.client() as s:
            err, data, _ = await self.call(s, "iterate", message="hi", gates=[{"type": "regex", "pattern": "^echo:[0-9]$"}, {"type": "length", "min_chars": 3}])
            self.assertFalse(err)
            self.assertTrue(data["passed"])
            self.assertEqual(data["text"], "echo:1")
            err, data, _ = await self.call(s, "iterate", message="hi", gates=[{"type": "contains", "all": ["never"]}], max_attempts=2)
            self.assertFalse(err)                                                        # exhausted attempts is a result, not an error
            self.assertFalse(data["passed"])
            self.assertEqual((len(data["attempts"]), data["text"]), (2, "echo:3"))     # retry carried assistant + feedback turns
            err, data, _ = await self.call(s, "iterate", message="hi", gates=[{"type": "nli", "source": "hi hi hi"}])
            self.assertTrue(data["passed"])                                              # fake NLI says entailment
            for args in ({"message": "x", "gates": [{"type": "shell"}]}, {"message": "x", "gates": []},
                         {"message": "x", "gates": [{"type": "length", "min_chars": 1}], "max_attempts": 99}):
                err, _, text = await self.call(s, "iterate", **args)
                self.assertTrue(err)
                self.assertIn("invalid_arguments", text)

    async def test_decide_single_multi_and_top_k(self):
        async with self.client() as s:
            err, data, _ = await self.call(s, "decide", state="s", question="q", options=["a", "b", "c", "d"], top_k=2)
            self.assertFalse(err)
            r = data["results"][0]
            self.assertEqual((data["model"], r["answer"], len(r["top"])), ("dec", "a", 2))
            self.assertEqual(r["top"][0], ["a", 0.6])
            err, data, _ = await self.call(s, "decide", state="s", question="q", options=["a", "b", "c", "d"], top_k=0)
            self.assertEqual(len(data["results"][0]["top"]), 4)                          # 0 = all
            err, data, _ = await self.call(s, "decide", state="s", questions=[
                {"t": "choice", "ins": "i", "crit": {"x": None, "y": None}}, {"t": "noul", "ins": "j"}])
            self.assertEqual([x["answer"] for x in data["results"]], ["x", "false"])
            for args in ({"state": "s"}, {"state": "s", "question": "q", "questions": []}):
                err, _, text = await self.call(s, "decide", **args)
                self.assertTrue(err)
                self.assertIn("invalid_arguments", text)

    async def test_speak_and_transcribe(self):
        async with self.client() as s:
            err, data, _ = await self.call(s, "speak", text="hello there", voice="af_heart", fresh=True)
            self.assertFalse(err)
            self.assertEqual((data["duration_s"], data["backend"], data["segments"][0]["end_s"]), (1.5, "voice", 1.5))
            self.assertEqual((data["echo"]["voice"], data["echo"]["fresh"], "instruct" in data["echo"]), ("af_heart", True, False))
            err, _, text = await self.call(s, "speak", text="")
            self.assertTrue(err)
            self.assertIn("text is empty", text)
            err, _, text = await self.call(s, "speak", text="x", model="llm")
            self.assertTrue(err)
            self.assertIn("wrong_model_kind", text)
            err, data, _ = await self.call(s, "transcribe", path="/x.wav")
            self.assertFalse(err)
            self.assertEqual([w["word"] for w in data["words"]], ["hello", "world"])

    async def test_narrate_and_voices(self):
        async with self.client() as s:
            err, data, _ = await self.call(s, "narrate", scenes=[{"id": "one", "text": "Hello [[w]] world."}, {"id": "two", "text": "Hello world."}])
            self.assertFalse(err)
            self.assertEqual([x["id"] for x in data["scenes"]], ["one", "two"])
            self.assertEqual((data["scenes"][0]["cues"], data["scenes"][1]["start_s"], data["total_s"]), ({"w": 0.5}, 1.5, 3.0))
            self.assertEqual(data["scenes"][0]["transcript_differs"], [])
            self.assertEqual((data["model"], data["stt_model"]), ("voice", "ears"))
            err, _, text = await self.call(s, "narrate", scenes=[{"id": "a b", "text": "x"}])
            self.assertTrue(err)
            self.assertIn("invalid_arguments", text)
            err, _, text = await self.call(s, "narrate", scenes=[{"id": "a", "text": "x"}], model="llm")
            self.assertIn("wrong_model_kind", text)
            err, data, _ = await self.call(s, "voices")
            self.assertEqual(([m["model"] for m in data["models"]], data["default_narrator"]), (["voice"], None))

    async def test_search(self):
        async with self.client() as s:
            err, data, _ = await self.call(s, "search", query="eta expansion", k=2, sources=["scala2/issues"], text_chars=50)
            self.assertFalse(err)
            self.assertEqual((data["backend"], len(data["results"]), data["results"][0]["source"]), ("find", 2, "scalac"))
            self.assertEqual(len(data["results"][0]["text"]), 50)                       # cut to what the caller asked for
            self.assertNotIn("truncated", data["results"][0])
            self.assertNotIn("color", data["results"][0])                               # presentation detail for the page, not for a model
            err, data, _ = await self.call(s, "search", query="x", linked_to="scala/bug#1", link_type=["closed_by"], has_link=["no_closed_by"], related=False)
            self.assertEqual(data["echo"]["related"], False)
            self.assertEqual((err, data["echo"]["linked_to"], data["echo"]["link_type"], data["echo"]["has_link"]), (False, "scala/bug#1", ["closed_by"], ["no_closed_by"]))
            err, data, _ = await self.call(s, "search", query="x", since="2024-01", authors=["retronym", "Jason Zaugg"])
            self.assertEqual((err, data["echo"]["since"], data["echo"]["authors"]), (False, "2024-01", ["retronym", "Jason Zaugg"]))
            err, data, _ = await self.call(s, "links", ref="scala/bug#1")
            self.assertEqual((err, data["found"]), (False, False))                      # the test backend has no index
            err, data, _ = await self.call(s, "search_universes")
            self.assertEqual((err, data["universes"], data["default"]), (False, [], None))   # the test backend has no config
            err, _, text = await self.call(s, "search", query="")
            self.assertTrue(err)
            self.assertIn("query", text)

    async def test_entail(self):
        async with self.client() as s:
            err, data, _ = await self.call(s, "entail", premise="p", hypotheses=["a", "b"])
            self.assertFalse(err)
            self.assertEqual([r["label"] for r in data["results"]], ["entailment", "entailment"])
            self.assertEqual(data["results"][0]["entailment"], 1.0)
            err, _, text = await self.call(s, "entail", premise="p", hypotheses=[])
            self.assertTrue(err)

    async def test_lifecycle_tools_require_the_token(self):
        for tool, args in (("start_backend", {"name": "llm"}), ("stop_backend", {"name": "llm"}), ("set_backend_policy", {"name": "llm", "ttl_s": 5})):
            async with self.client() as s:                                               # no token
                err, _, text = await self.call(s, tool, **args)
                self.assertTrue(err)
                self.assertIn("unauthorized", text)
            async with self.client(token="wrong") as s:
                err, _, text = await self.call(s, tool, **args)
                self.assertIn("unauthorized", text)
        self.assertEqual((self.sup.rt["llm"].starts, self.sup.rt["llm"].ttl_s), (0, 600.0))     # nothing changed

    async def test_lifecycle_with_the_token(self):
        async with self.client(token=TOKEN) as s:
            err, data, _ = await self.call(s, "start_backend", name="llm")
            self.assertFalse(err)
            self.assertEqual((data["name"], data["state"]), ("llm", "ready"))
            err, data, _ = await self.call(s, "set_backend_policy", name="llm", ttl_s=42, pinned=True)
            self.assertEqual((data["ttl_s"], data["pinned"]), (42.0, True))
            err, _, text = await self.call(s, "set_backend_policy", name="llm", ttl_s=-1)
            self.assertTrue(err)
            err, data, _ = await self.call(s, "set_backend_policy", name="llm", pinned=False)
            err, data, _ = await self.call(s, "stop_backend", name="llm")
            self.assertEqual(data["state"], "stopped")
            err, _, text = await self.call(s, "start_backend", name="nope")
            self.assertTrue(err)
            self.assertIn("model_not_found", text)

    async def test_start_that_cannot_fit_reports_why_and_pinning_respects_the_budget(self):
        async with self.client(token=TOKEN) as s:
            await self.call(s, "start_backend", name="llm")                              # 6 of 10 GB
            await self.call(s, "set_backend_policy", name="llm", pinned=True)
            err, _, text = await self.call(s, "start_backend", name="llm2")              # 6 more would not fit, llm is pinned
            self.assertTrue(err)
            self.assertIn("insufficient_memory", text)
            self.assertIn("llm (pinned)", text)
            err, _, text = await self.call(s, "set_backend_policy", name="llm2", pinned=True)   # 12 GB pinned > 10 GB budget
            self.assertTrue(err)
            self.assertIn("more than the 10 GB budget", text)

    async def test_host_policy_covers_the_mcp_endpoint(self):
        import httpx
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(self.url, json={}, headers={"Host": "evil.example", "Accept": "application/json, text/event-stream"})
            self.assertEqual(r.status_code, 421)
            r = await c.post(self.url, json={}, headers={"Origin": "https://evil.example", "Accept": "application/json, text/event-stream"})
            self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
