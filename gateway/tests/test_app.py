import asyncio, json, socket, sqlite3, tempfile, time, unittest
from pathlib import Path

import httpx
import uvicorn

from gateway.app import create_app
from gateway.catalog import parse
from gateway.core import ApiError, search_stats
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
            "find": {"kind": "search"},
            "lms": {"kind": "score"},
            "voice": {"kind": "tts"},
            "paint": {"kind": "image", "aliases": ["image"]},
            "ears": {"kind": "stt"},
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
    async def test_speech_routes_proxy_to_the_tts_backend(self):
        r = await self.http.post("/v1/audio/speech", json={"input": "hello", "voice": "af_heart"})
        self.assertEqual((r.status_code, r.headers["content-type"], r.content), (200, "audio/wav", b"RIFFfakewav"))
        self.assertEqual(r.headers["x-gateway-backend"], "voice")
        r = await self.http.post("/api/speak", json={"text": "hello", "voice": "bm_george"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["duration_s"], r.json()["echo"]["voice"]), (1.5, "bm_george"))
        self.assertNotIn("model", r.json()["echo"])                                  # routing key is not forwarded
        r = await self.http.post("/api/speak", json={"text": ""})
        self.assertEqual(r.status_code, 400)
        r = await self.http.post("/api/speak", json={"text": "x", "model": "llm"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "wrong_model_kind"))

    async def test_image_routes(self):
        r = await self.http.post("/api/image", json={"prompt": "a cat", "width": 512, "seed": 3})
        self.assertEqual((r.status_code, r.headers["x-gateway-backend"]), (200, "paint"))
        self.assertEqual((r.json()["width"], r.json()["echo"]["seed"]), (512, 3))
        self.assertNotIn("model", r.json()["echo"])
        self.assertEqual((await self.http.post("/api/image", json={"prompt": ""})).status_code, 400)
        r = await self.http.post("/api/image", json={"prompt": "x", "model": "llm"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "wrong_model_kind"))
        r = await self.http.get("/api/image/models")
        self.assertEqual([(m["model"], m["aliases"]) for m in r.json()["models"]], [("paint", ["image"])])

    async def test_image_file_is_served_from_the_output_dir_only(self):
        out = Path(self.tmp.name)
        self.cat.backends["paint"].options["output_dir"] = str(out)               # the fake backend is a `command` one: no adapter default
        (out / "a-1.png").write_bytes(b"\x89PNGfake")
        r = await self.http.get("/api/image/file/a-1.png")
        self.assertEqual((r.status_code, r.headers["content-type"], r.content), (200, "image/png", b"\x89PNGfake"))
        for bad in ("missing.png", "..%2Fsecret.png", "a-1.txt"):
            self.assertEqual((await self.http.get("/api/image/file/" + bad)).status_code, 404, bad)

    async def test_transcribe_route_and_voices_listing(self):
        r = await self.http.post("/api/transcribe", json={"path": "/x.wav"})
        self.assertEqual((r.status_code, r.json()["text"]), (200, "hello world"))
        r = await self.http.get("/api/voices")
        self.assertEqual([m["model"] for m in r.json()["models"]], ["voice"])
        self.assertEqual(self.sup.rt["llm"].starts, 0)

    async def test_narrate_route(self):
        r = await self.http.post("/api/narrate", json={"scenes": [{"id": "s1", "text": "Hello, [[two]] world."}], "speed": 1.1})
        self.assertEqual(r.status_code, 200)
        sc = r.json()["scenes"][0]
        self.assertEqual((sc["text"], sc["cues"], sc["duration_s"], sc["file"]), ("Hello, world.", {"two": 0.5}, 1.5, "fake.wav"))
        r = await self.http.post("/api/narrate", json={"scenes": []})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_arguments"))
        r = await self.http.post("/api/narrate", json={"scenes": [{"text": "x"}], "speed": "fast"})
        self.assertEqual(r.status_code, 400)

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
        self.assertEqual((r.status_code, r.json()["answer"], r.headers["x-gateway-backend"]), (200, "a", "dec"))
        self.assertEqual(r.json()["echo"]["options"], ["a", "b"])                     # body passed through untouched
        r = await self.http.post("/api/decide", json={"model": "dec", "state": "s", "questions": [{"t": "noul", "ins": "i"}] * 2})
        self.assertEqual([x["answer"] for x in r.json()], ["false", "false"])          # "questions" -> /score_many
        r = await self.http.post("/api/decide", json={"state": "s", "question": "bad"})
        self.assertEqual((r.status_code, r.json()["error"]), (400, "QuestionError: bad"))   # backend 4xx passed through
        r = await self.http.post("/api/entail", json={"premise": "p", "hypotheses": ["a", "b", "c"]})
        self.assertEqual((r.status_code, len(r.json()["probs"]), r.headers["x-gateway-backend"]), (200, 3, "nli"))
        r = await self.http.post("/api/entail", json={"model": "llm", "premise": "p", "hypotheses": ["a"]})
        self.assertEqual(r.status_code, 400)                                          # llm is not an nli backend

    async def test_search_routes_validate_and_pass_through(self):
        r = await self.http.post("/api/search", json={"query": "eta expansion", "k": 3, "sources": ["scala2/issues"], "projects": ["scala2"], "kinds": ["comment"], "rerank": False})
        d = r.json()
        self.assertEqual((r.status_code, d["backend"], len(d["results"]), d["reranked"]), (200, "find", 3, False))
        self.assertEqual((d["echo"]["sources"], d["echo"]["projects"], d["echo"]["kinds"], d["echo"]["rerank"], d["echo"]["mode"]), (["scala2/issues"], ["scala2"], ["comment"], False, "hybrid"))
        r = await self.http.post("/api/search", json={"query": "q", "since": "2024", "until": "2025-06", "date": "updated", "authors": ["retronym"]})
        self.assertEqual((r.status_code, [r.json()["echo"][x] for x in ("since", "until", "date", "authors")]), (200, ["2024", "2025-06", "updated", ["retronym"]]))
        r = await self.http.post("/api/search", json={"query": "q", "explain": True})
        self.assertEqual((r.status_code, r.json()["echo"]["explain"]), (200, True))
        r = await self.http.post("/api/search", json={"query": "q", "text_chars": 5000})
        self.assertEqual((r.status_code, r.json()["echo"]["text_chars"]), (200, 5000))                  # the chunk text is cut to this many characters (default 1200)
        r = await self.http.post("/api/search", json={"query": "q", "bogus": 1})
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (400, "invalid_arguments"))
        r = await self.http.post("/api/search", json={"query": ""})
        self.assertEqual(r.status_code, 400)                                          # the backend's 400 is surfaced, not a 502
        r = await self.http.post("/api/search", json={"query": 5})
        self.assertEqual(r.status_code, 400)
        r = await self.http.post("/api/search", json={"query": "q", "model": "llm"})
        self.assertEqual(r.status_code, 400)                                          # llm is not a search backend

    async def test_search_ask_route_validates_and_runs_the_controller_in_a_thread(self):
        r = await self.http.post("/api/search/ask", json={"question": "q?", "bogus": 1})
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (400, "invalid_arguments"))
        r = await self.http.post("/api/search/ask", json={"question": "which PR fixed it?"})
        self.assertEqual(r.status_code, 503)                                            # the test backend has no index
        import threading, types
        from unittest import mock
        from gateway import core, searchinfo
        seen = {}
        def fake_ask(question, universe, k, rounds):
            seen.update(question=question, universe=universe, k=k, rounds=rounds, thread=threading.current_thread() is not threading.main_thread())
            return {"plan": {}, "answers": [{"ref": "p/x", "line": "confirmed: x"}], "trace": [{"round": 1}], "seconds": 1.0}
        mod = types.SimpleNamespace(ask=fake_ask, gw=types.SimpleNamespace(BASE="unset"), SEARCH_URL=None)
        cfg = types.SimpleNamespace(universe=lambda u: (_ for _ in ()).throw(KeyError(f"no universe {u}")) if u == "nope" else types.SimpleNamespace(id=u or "default-u"))
        spec = types.SimpleNamespace(name="find", options={"index_dir": "/x"})
        with mock.patch.object(core, "_search_config", return_value=(spec, cfg)), mock.patch.object(searchinfo, "_module", return_value=mod):
            r = await self.http.post("/api/search/ask", json={"question": "  which PR fixed it? ", "k": 3, "trace": False})
            d = r.json()
            self.assertEqual((r.status_code, d["universe"], d["answers"][0]["ref"], "trace" in d), (200, "default-u", "p/x", False))
            self.assertEqual(seen, {"question": "which PR fixed it?", "universe": "default-u", "k": 3, "rounds": 2, "thread": True})
            self.assertEqual(mod.SEARCH_URL, f"http://127.0.0.1:{self.port}")             # the controller calls this gateway back
            for bad in ({"question": "x"}, {"question": "which?", "k": 50}, {"question": "which?", "rounds": 0}):
                self.assertEqual((await self.http.post("/api/search/ask", json=bad)).status_code, 400, bad)
            r = await self.http.post("/api/search/ask", json={"question": "which?", "universe": "nope"})
            self.assertEqual(r.status_code, 404)
            mod.ask = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("POST /api/decide: 503 no room"))
            r = await self.http.post("/api/search/ask", json={"question": "which?"})
            self.assertEqual((r.status_code, r.json()["error"]["type"]), (502, "backend_error"))

    async def test_search_get_route_validates_and_reports_missing_index(self):
        r = await self.http.post("/api/search/get", json={"refs": ["p/issues:issue:1"]})
        self.assertEqual((r.status_code, r.json()["results"][0]["found"]), (200, False))        # the test backend has no index
        for bad in ({"refs": "p/x"}, {"refs": ["p/x"], "bogus": 1}, {}):
            r = await self.http.post("/api/search/get", json=bad)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_arguments"), bad)

    async def test_search_links_route_and_the_link_filters(self):
        r = await self.http.post("/api/search/links", json={"ref": "scala/bug#1"})
        self.assertEqual((r.status_code, r.json()["found"]), (200, False))            # the test backend has no index
        for bad in ({"ref": 5}, {"ref": "x", "bogus": 1}, {}):
            r = await self.http.post("/api/search/links", json=bad)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_arguments"), bad)
        r = await self.http.post("/api/search", json={"query": "q", "linked_to": "#1", "link_type": ["closed_by"], "has_link": ["no_closed_by"], "refs_in_query": False, "related": 3, "link_boost": 0.5})
        self.assertEqual(r.status_code, 200)
        echo = r.json()["echo"]
        self.assertEqual((echo["related"], echo["link_boost"]), (3, 0.5))
        self.assertEqual((echo["linked_to"], echo["link_type"], echo["has_link"], echo["refs_in_query"]), ("#1", ["closed_by"], ["no_closed_by"], False))

    async def test_embeddings_are_openai_shaped_and_rerank_forwards(self):
        r = await self.http.post("/v1/embeddings", json={"input": ["ab", "abcd"], "model": "find", "kind": "query"})
        d = r.json()
        self.assertEqual((r.status_code, d["object"], d["model"]), (200, "list", "fake-embed"))
        self.assertEqual([(x["index"], x["embedding"]) for x in d["data"]], [(0, [2.0, 1.0]), (1, [4.0, 1.0])])
        r = await self.http.post("/api/rerank", json={"query": "q", "documents": ["a", "b"]})
        self.assertEqual((r.status_code, r.json()["scores"], r.headers["x-gateway-backend"]), (200, [1.0, 0.5], "find"))

    async def test_search_status_reads_config_and_indexes_without_starting_anything(self):
        r = await self.http.get("/api/search/status")
        self.assertEqual((r.status_code, r.json()["indexed"]), (200, False))          # the test backend has no index
        root = Path(self.tmp.name)
        cfgdir = root / "cfg"
        (cfgdir / "projects").mkdir(parents=True); (cfgdir / "universes").mkdir()
        (cfgdir / "search.json").write_text(json.dumps({"data_dir": str(root / "data")}))
        src = lambda sid, **kw: {"id": sid, "type": "git", "label": f"label {sid}", "color": "#112233", "priority": 2, "repo": "o/r", "ref": "main", "paths": ["."],
                                 "chunkers": {".scala": "scala"}, **kw}
        (cfgdir / "projects" / "p.json").write_text(json.dumps({"id": "p", "title": "Proj", "sources": [
            src("code"), {"id": "issues", "type": "github", "label": "label issues", "color": "#112233", "priority": 2, "repo": "o/r", "include": ["issues"]}, src("later"),
            {"id": "commits", "type": "git_log", "label": "label commits", "color": "#112233", "priority": 2, "repo": "o/r", "ref": "main", "since": "2020-01-01T00:00:00Z"}]}))
        (cfgdir / "projects" / "q.json").write_text(json.dumps({"id": "q", "title": "Not yet", "sources": [src("code")]}))
        (cfgdir / "universes" / "u.json").write_text(json.dumps({"id": "u", "title": "Uni", "projects": ["p", "q"], "default": True}))
        db = root / "data" / "projects" / "p" / "index.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.executescript("""CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, source TEXT, hash TEXT, updated REAL);
                             CREATE TABLE vec(rowid INTEGER PRIMARY KEY, model TEXT, hash TEXT); CREATE TABLE state(source TEXT, k TEXT, v TEXT);
                             INSERT INTO chunks VALUES (1,'code','h1',5),(2,'code','h2',6),(3,'issues','h3',7);
                             INSERT INTO vec VALUES (1,'Qwen/Qwen3-Embedding-0.6B','h1'),(2,'Qwen/Qwen3-Embedding-0.6B','stale');
                             INSERT INTO state VALUES ('issues','since_issues','2026-01-01T00:00:00Z');
                             INSERT INTO state VALUES ('commits','top_date','2026-01-01T00:00:00Z'),('commits','back_date','2023-01-01T00:00:00Z'),('commits','head','abcdef0123456789');""")
        con.commit(); con.close()
        cat = parse({"backends": {"s": {"adapter": "search", "python": "py", "index_dir": str(Path(__file__).parents[2] / "pipelines" / "search"), "config_dir": str(cfgdir),
                                        "est_mem_gb": 1}}}, Path("/base"))
        d = search_stats(cat)
        self.assertEqual((d["digest"], d["refresh"]), (None, None))                                       # nothing refreshed yet
        (root / "data" / "digest.json").write_text(json.dumps({"universe": "u", "generated": 1.0, "since": "2026-01-01T00:00:00Z", "model": "m", "checked": True, "attempts": 1, "facts": 3, "text": "A digest.", "source_facts": "x"}))
        (root / "data" / "refresh.json").write_text(json.dumps({"last_run": {"ok": True, "phases": {"sync": {"ok": True, "secs": 1.0}}}, "history": [{"ok": True}] * 8}))
        d = search_stats(cat)
        self.assertEqual((d["digest"]["text"], d["digest"]["checked"], "source_facts" in d["digest"]), ("A digest.", True, False))   # the facts stay on disk
        self.assertEqual((d["refresh"]["last_run"]["ok"], len(d["refresh"]["history"])), (True, 5))
        self.assertEqual((d["universe"]["id"], d["indexed"], [u["id"] for u in d["universes"]]), ("u", True, ["u"]))
        p, q = d["projects"]
        self.assertEqual((p["id"], p["indexed"], q["indexed"]), ("p", True, False))                       # q has no database yet
        by = {s["id"]: s for s in p["sources"]}
        self.assertEqual((by["code"]["chunks"], by["code"]["embedded"], by["code"]["color"], by["code"]["label"]), (2, 1, "#112233", "label code"))   # a stale vector does not count
        self.assertEqual((by["issues"]["chunks"], by["issues"]["position"]), (1, "2026-01-01T00:00:00Z"))
        self.assertEqual(by["later"]["chunks"], 0)                                                         # configured but empty
        c = by["commits"]                                                                                  # the commit backfill: (top - back) / (top - horizon) = 3 of 6 years
        self.assertEqual((c["sync"]["streams"][0]["name"], c["sync"]["streams"][0]["mode"], c["position"]), ("commits", "back", "abcdef0123"))
        self.assertAlmostEqual(c["sync"]["frac"], 0.5, delta=0.02)
        self.assertEqual(d["universes"][0]["projects"][0]["sources"][0]["key"], "p/code")
        self.assertEqual([s["kinds"] for s in d["universes"][0]["projects"][0]["sources"]], [["file"], ["issue"], ["file"], ["commit"]])
        with self.assertRaises(ApiError):
            search_stats(cat, universe="nope")

    async def test_duplicates_and_clusters_routes_parse_their_query_and_report_missing_data(self):
        for path in ("/api/search/duplicates", "/api/search/clusters", "/api/search/outliers", "/api/search/dashboard"):
            r = await self.http.get(path)
            self.assertEqual((r.status_code, r.json()["available"]), (200, False))                       # the test backend has no index, so nothing is computed
        r = await self.http.get("/api/search/duplicates?state=open&kind=mixed&since=2024&until=2025-06&projects=a&projects=b&min_sim=0.85&adjacent=0&templated=1&limit=10&offset=5")
        self.assertEqual(r.status_code, 200)
        r = await self.http.get("/api/search/clusters?state=closed&kind=pr&cluster=3&limit=9999")
        self.assertEqual(r.status_code, 200)
        r = await self.http.get("/api/search/dashboard?since=2026-09-01&until=2026-10-01&projects=a&projects=b&universe=u")
        self.assertEqual(r.status_code, 200)
        r = await self.http.get("/api/search/outliers?by=ctr&state=open&kind=issue&since=2020&until=2024-06&projects=a&templated=1&limit=20&offset=40")
        self.assertEqual(r.status_code, 200)
        for path in ("/api/search/duplicates?min_sim=high", "/api/search/duplicates?adjacent=x", "/api/search/duplicates?templated=maybe", "/api/search/duplicates?limit=many",
                     "/api/search/clusters?cluster=first", "/api/search/clusters?offset=-x", "/api/search/outliers?templated=maybe", "/api/search/outliers?limit=lots"):
            r = await self.http.get(path)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_arguments"), path)

    async def test_score_routes_to_the_score_backend(self):
        r = await self.http.post("/api/score", json={"prompt": "p", "candidates": [" a", " bb"]})
        self.assertEqual((r.status_code, r.json()["logprobs"], r.headers["x-gateway-backend"]), (200, [-2.0, -3.0], "lms"))
        r = await self.http.post("/api/score", json={"model": "dec", "prompt": "p", "candidates": [" a"]})
        self.assertEqual(r.status_code, 400)                                          # dec is not a score backend

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
        self.assertEqual([b["name"] for b in j["backends"]], ["llm", "dec", "nli", "find", "lms", "voice", "paint", "ears", "bad"])
        self.assertEqual((j["gateway"]["memory_budget_gb"], j["gateway"]["resident_est_gb"], j["gateway"]["default_llm"]), (100, 0, "llm"))
        await self.http.post("/v1/chat/completions", json=self.chat())
        j = (await self.http.get("/api/backends")).json()
        self.assertEqual(j["gateway"]["resident_est_gb"], 1)
        self.assertEqual({b["name"]: b["state"] for b in j["backends"]}["llm"], "ready")


class BudgetHttpTests(unittest.IsolatedAsyncioTestCase):
    """Memory budget as seen by HTTP clients (budget 10 GB, backends 6 GB each)."""

    async def start_gateway(self, backends):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.cat = catalog(backends, port=self.port, memory_budget_gb=10, room_timeout_s=0.4, default_llm="x")
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

    def chat(self, model):
        return {"model": model, "messages": [{"role": "user", "content": "hi"}]}

    async def test_second_model_evicts_the_first_and_status_shows_it(self):
        await self.start_gateway({"x": {"kind": "llm", "est_mem_gb": 6}, "y": {"kind": "llm", "est_mem_gb": 6}})
        self.assertEqual((await self.http.post("/v1/chat/completions", json=self.chat("x"))).status_code, 200)
        self.assertEqual((await self.http.post("/v1/chat/completions", json=self.chat("y"))).status_code, 200)   # evicts x
        j = (await self.http.get("/api/backends")).json()
        self.assertEqual({b["name"]: b["state"] for b in j["backends"]}, {"x": "stopped", "y": "ready"})
        self.assertEqual((j["gateway"]["resident_est_gb"], j["gateway"]["free_est_gb"]), (6.0, 4.0))
        self.assertIn("free_pct", j["gateway"]["system"])

    async def test_pinned_backend_makes_the_other_a_503_with_retry_after(self):
        await self.start_gateway({"x": {"kind": "llm", "est_mem_gb": 6, "pinned": True}, "y": {"kind": "llm", "est_mem_gb": 6}})
        self.assertEqual((await self.http.post("/v1/chat/completions", json=self.chat("x"))).status_code, 200)
        r = await self.http.post("/v1/chat/completions", json=self.chat("y"))
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (503, "insufficient_memory"))
        self.assertIn("x (pinned)", r.json()["error"]["message"])
        self.assertEqual(r.headers["retry-after"], "10")
        self.assertEqual(self.sup.rt["x"].state, State.READY)                          # the pinned one is untouched


if __name__ == "__main__":
    unittest.main()
