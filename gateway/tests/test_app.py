import asyncio, json, socket, tempfile, time, unittest
from pathlib import Path

import httpx
import uvicorn

from gateway.app import create_app
from gateway.supervisor import State, Supervisor
from gateway.tests.test_supervisor import catalog, until


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.cat = catalog({
            "llm": {"kind": "llm", "aliases": ["org/some-model"], "flags": ["--ready-delay", "0.3"]},
            "dec": {"kind": "decision"},
            "nli": {"kind": "nli"},
            "bad": {"kind": "llm", "flags": ["--die-on-start"]},
        }, port=self.port, default_llm="llm")
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

    def chat(self, **kw):
        return {"messages": [{"role": "user", "content": "hi"}], **kw}

    # ----------------------------------------------------------------------------------------------------------
    async def test_models_lists_llm_backends_without_starting_anything(self):
        r = await self.http.get("/v1/models")
        self.assertEqual(r.status_code, 200)
        data = {m["id"]: m for m in r.json()["data"]}
        self.assertEqual(set(data), {"llm", "bad"})                                  # only llm-kind backends
        self.assertEqual((data["llm"]["state"], data["llm"]["ready"]), ("stopped", False))
        self.assertIn("org/some-model", data["llm"]["aliases"])
        self.assertEqual(self.sup.rt["llm"].starts, 0)

    async def test_chat_lazy_start_alias_rewrite_and_cold_header(self):
        r = await self.http.post("/v1/chat/completions", json=self.chat(model="org/some-model"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "echo:1")
        self.assertEqual(r.json()["model"], "llm")                                   # alias rewritten to the backend's own name
        self.assertEqual(r.headers["x-gateway-backend"], "llm")
        self.assertGreater(float(r.headers["x-gateway-cold-start-secs"]), 0.2)        # we waited for the lazy start
        r2 = await self.http.post("/v1/chat/completions", json=self.chat())          # no model: default_llm
        self.assertEqual(r2.status_code, 200)
        self.assertNotIn("x-gateway-cold-start-secs", r2.headers)
        self.assertEqual(self.sup.rt["llm"].starts, 1)

    async def test_concurrent_cold_requests_share_one_start(self):
        rs = await asyncio.gather(*[self.http.post("/v1/chat/completions", json=self.chat()) for _ in range(4)])
        self.assertEqual([r.status_code for r in rs], [200] * 4)
        self.assertEqual(self.sup.rt["llm"].starts, 1)

    async def test_stream_is_incremental_and_holds_the_lease(self):
        await self.sup.start("llm")
        self.sup.set_policy("llm", ttl_s=0.2)                                         # would passivate quickly if idle
        times, pending = [], []
        t0 = time.monotonic()
        async with self.http.stream("POST", "/v1/chat/completions", json=self.chat(stream=True, n_chunks=5, delay=0.3)) as r:
            self.assertEqual(r.status_code, 200)
            async for line in r.aiter_lines():
                if line.startswith("data: {"):
                    times.append(time.monotonic() - t0)
                    pending.append((self.sup.rt["llm"].pending, self.sup.rt["llm"].state))
        self.assertEqual(len(times), 5)
        self.assertTrue(all(b - a > 0.2 for a, b in zip(times, times[1:])), times)    # arrived spread out, not buffered
        self.assertTrue(all(p == (1, State.READY) for p in pending), pending)         # kept alive while streaming
        self.assertTrue(await until(lambda: self.sup.rt["llm"].state is State.STOPPED, 3))   # idles out afterwards

    async def test_client_disconnect_releases_the_lease(self):
        await self.sup.start("llm")
        async with self.http.stream("POST", "/v1/chat/completions", json=self.chat(stream=True, n_chunks=100, delay=0.2)) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: {"):
                    break                                                             # leave early: closes the connection
        self.assertTrue(await until(lambda: self.sup.rt["llm"].pending == 0 and self.sup.rt["llm"].in_flight == 0, 3))

    async def test_error_shapes(self):
        r = await self.http.post("/v1/chat/completions", json=self.chat(model="nope"))
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (404, "model_not_found"))
        r = await self.http.post("/v1/chat/completions", json=self.chat(model="dec"))
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (400, "wrong_model_kind"))
        r = await self.http.post("/v1/chat/completions", content=b"{nope", headers={"content-type": "application/json"})
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (400, "invalid_json"))
        r = await self.http.post("/v1/chat/completions", content=b"[1]", headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 400)

    async def test_backend_start_failure_is_503_then_backoff_with_retry_after(self):
        r = await self.http.post("/v1/chat/completions", json=self.chat(model="bad"))
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (503, "backend_start_failed"))
        self.assertIn("exited with code 3", r.json()["error"]["message"])
        r = await self.http.post("/v1/chat/completions", json=self.chat(model="bad"))
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (503, "backend_unavailable"))
        self.assertIn("retry-after", r.headers)

    async def test_decide_and_entail_route_by_kind(self):
        r = await self.http.post("/api/decide", json={"state": "s", "question": "q", "options": ["a", "b"]})
        self.assertEqual((r.status_code, r.json()["answer"], r.headers["x-gateway-backend"]), (200, "x", "dec"))
        self.assertEqual(r.json()["echo"]["options"], ["a", "b"])                     # body passed through untouched
        r = await self.http.post("/api/decide", json={"model": "dec", "state": "s", "questions": [{"t": "noul", "ins": "i"}] * 2})
        self.assertEqual([x["answer"] for x in r.json()], ["y", "y"])                 # "questions" -> /score_many
        r = await self.http.post("/api/decide", json={"state": "s", "question": "bad"})
        self.assertEqual((r.status_code, r.json()["error"]), (400, "QuestionError: bad"))   # backend 4xx passed through
        r = await self.http.post("/api/entail", json={"premise": "p", "hypotheses": ["a", "b", "c"]})
        self.assertEqual((r.status_code, len(r.json()["probs"]), r.headers["x-gateway-backend"]), (200, 3, "nli"))
        r = await self.http.post("/api/entail", json={"model": "llm", "premise": "p", "hypotheses": ["a"]})
        self.assertEqual(r.status_code, 400)                                          # llm is not an nli backend

    async def test_host_and_origin_policy(self):
        r = await self.http.get("/healthz", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 421)
        r = await self.http.get("/healthz", headers={"Host": "127.0.0.1:1"})            # right name, wrong port
        self.assertEqual(r.status_code, 421)
        r = await self.http.get("/healthz", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = await self.http.get("/healthz", headers={"Origin": "http://localhost:5173"})  # other localhost page: allowed + CORS
        self.assertEqual((r.status_code, r.headers["access-control-allow-origin"]), (200, "http://localhost:5173"))
        r = await self.http.get("/healthz")                                              # non-browser client: no CORS headers
        self.assertEqual((r.status_code, "access-control-allow-origin" in r.headers), (200, False))
        r = await self.http.options("/v1/chat/completions", headers={"Origin": "http://127.0.0.1:8765", "Access-Control-Request-Method": "POST"})
        self.assertEqual(r.status_code, 204)
        self.assertIn("POST", r.headers["access-control-allow-methods"])
        r = await self.http.options("/v1/chat/completions", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = await self.http.post("/v1/chat/completions", json=self.chat(), headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.sup.rt["llm"].starts, 0)                                   # rejected BEFORE any backend was started

    async def test_backends_status(self):
        r = await self.http.get("/api/backends")
        j = r.json()
        self.assertEqual([b["name"] for b in j["backends"]], ["llm", "dec", "nli", "bad"])
        self.assertEqual((j["gateway"]["memory_budget_gb"], j["gateway"]["resident_est_gb"], j["gateway"]["default_llm"]), (100, 0, "llm"))
        await self.http.post("/v1/chat/completions", json=self.chat())
        j = (await self.http.get("/api/backends")).json()
        self.assertEqual(j["gateway"]["resident_est_gb"], 1)
        self.assertEqual({b["name"]: b["state"] for b in j["backends"]}["llm"], "ready")


if __name__ == "__main__":
    unittest.main()
