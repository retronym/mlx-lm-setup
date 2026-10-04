import asyncio, socket, sys, tempfile, unittest
from pathlib import Path

import httpx
import uvicorn

from gateway.app import create_app
from gateway.catalog import parse
from gateway.supervisor import Supervisor
from gateway.tests.test_supervisor import FAKE, _next_port


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProfileHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        base = _next_port[0]
        _next_port[0] += 10
        cmd = lambda *extra: [sys.executable, FAKE, "--port", "{port}", *extra]
        self.cat = parse({
            "gateway": {"port": self.port, "backend_port_base": base, "memory_budget_gb": 100, "default_llm": "llm"},
            "backends": {
                "llm": {"adapter": "command", "command": cmd(), "kind": "llm", "est_mem_gb": 1},
                "dec": {"adapter": "command", "command": cmd(), "kind": "decision", "est_mem_gb": 1},
            },
            "profiles": {
                "think": {"backend": "llm", "description": "reasoning on", "aliases": ["deep"], "system_prompt": "Be careful.",
                          "defaults": {"max_tokens": 4096, "temperature": 0.6, "chat_template_kwargs": {"enable_thinking": True}}},
                "tune": {"backend": "dec", "defaults": {"temperature": 1.5}},
            },
        }, Path(self.tmp.name))
        self.sup = Supervisor(self.cat, state_dir=Path(self.tmp.name) / "state", reap_interval=0.05, health_interval=0.05, grace_s=0.5)
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.cat, supervisor=self.sup), host="127.0.0.1", port=self.port,
                                                    log_level="warning", lifespan="on"))
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", trust_env=False, timeout=30)

    async def asyncTearDown(self):
        await self.http.aclose()
        self.server.should_exit = True
        await self.task
        await self.sup.shutdown()
        self.tmp.cleanup()

    async def chat(self, **body):
        r = await self.http.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], **body})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["received"]

    async def test_profile_defaults_reach_the_backend(self):
        got = await self.chat(model="think")
        self.assertEqual(got["model"], "llm")                                           # profile resolved to its backend
        self.assertEqual((got["max_tokens"], got["temperature"]), (4096, 0.6))
        self.assertEqual(got["chat_template_kwargs"], {"enable_thinking": True})
        self.assertEqual(got["messages"][0], {"role": "system", "content": "Be careful."})
        self.assertEqual(got["messages"][1]["content"], "hi")

    async def test_client_values_win_and_alias_works(self):
        got = await self.chat(model="deep", temperature=0, max_tokens=10, chat_template_kwargs={"enable_thinking": False},
                              messages=[{"role": "system", "content": "mine"}, {"role": "user", "content": "q"}])
        self.assertEqual((got["temperature"], got["max_tokens"]), (0, 10))
        self.assertEqual(got["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(got["messages"][0]["content"], "mine")                         # no profile system prompt added
        self.assertEqual(len(got["messages"]), 2)

    async def test_plain_backend_name_gets_no_defaults(self):
        got = await self.chat(model="llm")
        for k in ("max_tokens", "temperature", "chat_template_kwargs"):
            self.assertNotIn(k, got)
        self.assertEqual(len(got["messages"]), 1)

    async def test_models_lists_profiles(self):
        data = {m["id"]: m for m in (await self.http.get("/v1/models")).json()["data"]}
        self.assertEqual(set(data), {"llm", "think"})                                   # decision-kind profile is not an LLM model
        self.assertEqual((data["think"]["profile_of"], data["think"]["description"], data["think"]["aliases"]),
                         ("llm", "reasoning on", ["deep"]))

    async def test_decision_profile_and_wrong_kind(self):
        r = await self.http.post("/api/decide", json={"model": "tune", "state": "s", "question": "q", "options": ["a", "b"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["echo"]["temperature"], 1.5)
        r = await self.http.post("/v1/chat/completions", json={"model": "tune", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (400, "wrong_model_kind"))


if __name__ == "__main__":
    unittest.main()
