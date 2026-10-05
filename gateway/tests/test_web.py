import asyncio, json, re, socket, tempfile, unittest
from pathlib import Path

import httpx
import uvicorn

from gateway.__main__ import admin_url
from gateway.app import WEB, create_app
from gateway.supervisor import State, Supervisor
from gateway.tests.test_supervisor import catalog, until

TOKEN = "web-test-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.cat = catalog({"llm": {"kind": "llm", "est_mem_gb": 6}, "other": {"kind": "llm", "est_mem_gb": 6}},
                           port=self.port, default_llm="llm", memory_budget_gb=10, room_timeout_s=0.4)
        self.sup = Supervisor(self.cat, state_dir=Path(self.tmp.name) / "state", reap_interval=0.05, health_interval=0.05, grace_s=0.5)
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.cat, supervisor=self.sup, token=TOKEN), host="127.0.0.1", port=self.port,
                                                    log_level="warning", lifespan="on"))
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", trust_env=False, timeout=30)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    async def asyncTearDown(self):
        await self.http.aclose()
        self.server.should_exit = True
        await self.task
        await self.sup.shutdown()
        self.tmp.cleanup()

    # ---- token-gated lifecycle API ----------------------------------------------------------------------------------
    async def test_lifecycle_endpoints_need_the_token(self):
        for path, body in (("/api/backends/llm/start", {}), ("/api/backends/llm/stop", {}), ("/api/backends/llm/policy", {"ttl_s": 5})):
            r = await self.http.post(path, json=body)
            self.assertEqual((r.status_code, r.json()["error"]["type"], r.headers["www-authenticate"]), (401, "unauthorized", "Bearer"))
            r = await self.http.post(path, json=body, headers={"Authorization": "Bearer nope"})
            self.assertEqual(r.status_code, 401)
        self.assertEqual((self.sup.rt["llm"].starts, self.sup.rt["llm"].ttl_s), (0, 600.0))

    async def test_start_policy_stop_with_the_token(self):
        r = await self.http.post("/api/backends/llm/start", headers=self.auth)
        self.assertEqual((r.status_code, r.json()["state"]), (200, "ready"))
        r = await self.http.post("/api/backends/llm/policy", json={"ttl_s": 42, "pinned": True}, headers=self.auth)
        self.assertEqual((r.json()["ttl_s"], r.json()["pinned"]), (42.0, True))
        r = await self.http.post("/api/backends/llm/policy", json={"pinned": False}, headers=self.auth)
        self.assertFalse(r.json()["pinned"])
        r = await self.http.post("/api/backends/llm/stop", headers=self.auth)                 # no body at all is fine
        self.assertEqual((r.status_code, r.json()["state"]), (200, "stopped"))
        r = await self.http.post("/api/backends/llm/stop", json={"force": True}, headers=self.auth)
        self.assertEqual(r.status_code, 200)                                                    # idempotent

    async def test_lifecycle_validation_errors(self):
        r = await self.http.post("/api/backends/nope/start", headers=self.auth)
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (404, "model_not_found"))
        for body in ({"ttl_s": -1}, {"ttl_s": "soon"}, {"ttl_s": True}, {"pinned": "yes"}):
            r = await self.http.post("/api/backends/llm/policy", json=body, headers=self.auth)
            self.assertEqual((r.status_code, r.json()["error"]["type"]), (400, "invalid_arguments"), body)
        await self.http.post("/api/backends/llm/policy", json={"pinned": True}, headers=self.auth)       # 6 GB pinned
        r = await self.http.post("/api/backends/other/policy", json={"pinned": True}, headers=self.auth)  # 12 > 10 GB budget
        self.assertEqual(r.status_code, 400)
        self.assertIn("more than the 10 GB budget", r.json()["error"]["message"])
        r = await self.http.post("/api/backends/llm/policy", content=b"{nope", headers={**self.auth, "content-type": "application/json"})
        self.assertEqual(r.status_code, 400)

    async def test_start_that_cannot_fit_is_503_for_the_admin_ui(self):
        await self.http.post("/api/backends/llm/start", headers=self.auth)
        await self.http.post("/api/backends/llm/policy", json={"pinned": True}, headers=self.auth)
        r = await self.http.post("/api/backends/other/start", headers=self.auth)
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (503, "insufficient_memory"))

    async def test_cross_site_pages_cannot_use_the_token_endpoints(self):
        r = await self.http.post("/api/backends/llm/start", headers={**self.auth, "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)                                                    # rejected before the token is even checked
        self.assertEqual(self.sup.rt["llm"].starts, 0)
        r = await self.http.post("/api/backends/llm/start", headers={**self.auth, "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(r.status_code, 200)                                                    # the admin page itself (same origin)

    # ---- SSE -------------------------------------------------------------------------------------------------------------
    async def sse(self, stream, seconds):
        """Collect (event, data) pairs from an SSE response for up to ``seconds``."""
        out, event = [], None

        async def reader():
            nonlocal event
            async for line in stream.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    out.append((event, json.loads(line[5:])))
        try:
            await asyncio.wait_for(reader(), seconds)
        except asyncio.TimeoutError:
            pass
        return out

    async def test_events_stream_snapshots_and_lifecycle_events_and_cleans_up(self):
        async def poke():
            await asyncio.sleep(0.5)
            await self.http.post("/api/backends/llm/start", headers=self.auth)
        t = asyncio.create_task(poke())
        async with self.http.stream("GET", "/api/events") as r:
            self.assertEqual((r.status_code, r.headers["content-type"].split(";")[0]), (200, "text/event-stream"))
            self.assertEqual(len(self.sup._subs), 1)
            evs = await self.sse(r, 3.2)
        await t
        kinds = [e for e, _ in evs]
        self.assertEqual(kinds[0], "snapshot")                                                  # first thing sent: current status
        first = evs[0][1]
        self.assertEqual([b["name"] for b in first["backends"]], ["llm", "other"])
        self.assertEqual(first["gateway"]["memory_budget_gb"], 10)
        self.assertGreaterEqual(kinds.count("snapshot"), 3)                                     # ticks about once a second
        logs = [d["kind"] for e, d in evs if e == "log"]
        self.assertIn("starting", logs)
        self.assertIn("ready", logs)
        last_snap = [d for e, d in evs if e == "snapshot"][-1]
        self.assertEqual({b["name"]: b["state"] for b in last_snap["backends"]}["llm"], "ready")
        self.assertTrue(await until(lambda: len(self.sup._subs) == 0, 3))                       # disconnect released the subscription

    async def test_events_replays_recent_history_flagged(self):
        await self.sup.start("llm")
        async with self.http.stream("GET", "/api/events") as r:
            evs = await self.sse(r, 0.6)
        replay = [d for e, d in evs if e == "log" and d.get("replay")]
        self.assertEqual([d["kind"] for d in replay], ["starting", "ready"])

    # ---- pages ----------------------------------------------------------------------------------------------------------
    async def test_pages_are_served_with_a_tight_policy(self):
        for path, marker in (("/", 'id="panels"'), ("/chat", 'id="model"'), ("/vision", 'id="drop"'), ("/translate", 'id="src"'), ("/search", 'id="q"'), ("/jev", 'id="magic"'), ("/speech", 'id="go"'), ("/admin", "/api/events")):
            r = await self.http.get(path)
            self.assertEqual((r.status_code, r.headers["content-type"].split(";")[0]), (200, "text/html"), path)
            self.assertIn(marker, r.text)
            csp = r.headers["content-security-policy"]
            self.assertIn("default-src 'none'", csp)
            self.assertIn("connect-src 'self'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertEqual((r.headers["x-frame-options"], r.headers["x-content-type-options"]), ("DENY", "nosniff"))
            self.assertEqual(r.headers["referrer-policy"], "no-referrer")

    async def test_home_shows_the_search_index_strip_from_the_status_endpoint(self):
        html = (WEB / "home.html").read_text()
        self.assertIn('fetch("/api/search/status")', html)
        self.assertIn('id="idxstrip"', html)

    async def test_the_digest_is_rendered_as_sanitised_markdown_with_the_vendored_libs(self):
        html = (WEB / "search.html").read_text()
        self.assertIn('<script src="/vendor/marked.min.js">', html)
        self.assertIn('<script src="/vendor/purify.min.js">', html)
        self.assertIn("DOMPurify.sanitize(marked.parse(", html)                        # model-written text containing GitHub titles is never inserted unsanitised
        self.assertIn('rel = "noopener noreferrer"', html)
        for name in ("marked.min.js", "purify.min.js"):
            r = await self.http.get(f"/vendor/{name}")
            self.assertEqual((r.status_code, r.headers["content-type"].split(";")[0]), (200, "text/javascript"))

    async def test_search_results_show_who_and_when_without_injecting_html(self):
        html = (WEB / "search.html").read_text()
        for needle in ("function relTime(", "function whoEl(", "function whom(", "GITHUB + encodeURIComponent(login)", 'rel = "noopener noreferrer"'):
            self.assertIn(needle, html)
        body = html[html.index("function whom("):html.index("function render(")]
        self.assertNotIn("innerHTML", body)                                            # names and handles come from GitHub: text nodes only

    async def test_search_page_has_duplicates_and_clusters_tabs_that_never_inject_html(self):
        html = (WEB / "search.html").read_text()
        for needle in ('id="t-duplicates"', 'id="t-clusters"', "/api/search/duplicates", "/api/search/clusters", 'id="nb-since"', 'id="nb-until"', 'id="nb-state"', 'id="nb-kind"',
                       'id="nb-import"', "function itemRow(", "function fromHash("):
            self.assertIn(needle, html)
        body = html[html.index("// ---- duplicates and clusters"):html.index('$("t-search").onclick')]
        self.assertNotIn("innerHTML", body)                                            # titles come from GitHub: text nodes, and links only to github.com
        self.assertIn("github\\.com", body)

    async def test_pages_load_nothing_from_other_origins(self):
        for name in ("chat.html", "admin.html", "jev.html", "speech.html", "translate.html", "search.html", "home.html"):
            html = (WEB / name).read_text()
            self.assertEqual(re.findall(r"""(?:src|href)\s*=\s*["']https?://""", html), [], name)     # no remote scripts/styles
            self.assertNotRegex(html, r"""url\(\s*["']?https?://""", name)
            self.assertNotRegex(html, r"""fetch\(\s*["']https?://""", name)

    async def test_pages_respect_the_host_policy(self):
        r = await self.http.get("/admin", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 421)

    def test_admin_url_carries_the_token_in_the_fragment_only(self):
        url = admin_url(self.cat, "secret-token")
        self.assertEqual(url, f"http://127.0.0.1:{self.port}/admin#secret-token".replace("#secret-token", "#token=secret-token"))
        self.assertNotIn("?", url)                                                              # a query string would be sent to the server


if __name__ == "__main__":
    unittest.main()
