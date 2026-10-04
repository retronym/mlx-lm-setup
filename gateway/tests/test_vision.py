import asyncio, base64, json, tempfile, unittest
from pathlib import Path

import httpx
import uvicorn

from gateway import vision
from gateway.app import create_app
from gateway.catalog import CatalogError, parse
from gateway.core import ApiError
from gateway.supervisor import Supervisor
from gateway.tests.test_app import free_port
from gateway.tests.test_supervisor import catalog

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
PDF = b"%PDF-1.4\n%fake\n"


class Inline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "b.png").write_bytes(PNG)
        (self.d / "a.png").write_bytes(PNG)
        (self.d / "doc.pdf").write_bytes(PDF)
        (self.d / "notes.txt").write_text("not an image")
        (self.d / "fake.png").write_text("a text file with an image name")

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_files_become_data_uris_sniffed_by_content(self):
        uri = vision.inline(str(self.d / "a.png"))
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(uri.split(",", 1)[1]), PNG)
        self.assertTrue(vision.inline(f"file://{self.d}/doc.pdf#page=3").startswith("data:application/pdf;base64,"))
        self.assertTrue(vision.inline(f"{self.d}/doc.pdf#page=3").endswith("#page=3"))
        self.assertEqual(vision.inline("data:image/png;base64,AAAA"), "data:image/png;base64,AAAA")

    def test_refused(self):
        for bad in ("https://example.com/x.png", "http://127.0.0.1:8090/x.png", "relative/a.png", str(self.d / "missing.png"),
                    str(self.d / "notes.txt"), str(self.d / "fake.png"), f"{self.d}/a.png#page=1", f"{self.d}/doc.pdf#page=0",
                    "data:text/html;base64,AAAA", ""):
            with self.assertRaises(ApiError, msg=bad):
                vision.inline(bad)

    def test_directories_expand_to_their_images_in_order(self):
        self.assertEqual(vision.expand([str(self.d), "data:image/png;base64,AA"]),
                         [str(self.d / "a.png"), str(self.d / "b.png"), str(self.d / "fake.png"), "data:image/png;base64,AA"])

    def test_presets_and_json(self):
        with self.assertRaises(ApiError):
            vision.preset_prompt("storyboard", None)
        self.assertIn("the title card", vision.preset_prompt("storyboard", "the title card"))
        self.assertNotIn("{context}", vision.preset_prompt("table", None))
        self.assertEqual(vision.parse_json('```json\n{"ok": false, "issues": []}\n```'), {"ok": False, "issues": []})
        self.assertIsNone(vision.parse_json("no json"))

    def test_catalog_adapter(self):
        base = {"gateway": {}, "backends": {"v": {"adapter": "mlx_vlm", "python": "/bin/python", "model": "org/m", "est_mem_gb": 1, "image_tokens": 560}}}
        s = parse(base, self.d).backends["v"]
        self.assertEqual(s.kind, "vision")
        self.assertEqual(s.command()[-4:], ["--image-tokens", "560", "--max-tokens", "1024"])
        base["backends"]["v"]["image_tokens"] = "lots"
        with self.assertRaises(CatalogError):
            parse(base, self.d)


class VisionRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        for n in ("s1.png", "s2.png"):
            (self.d / "stills").mkdir(exist_ok=True)
            (self.d / "stills" / n).write_bytes(PNG)
        (self.d / "paper.pdf").write_bytes(PDF)
        self.port = free_port()
        self.cat = catalog({"llm": {"kind": "llm"}, "eyes": {"kind": "vision", "aliases": ["vision"]}, "nli": {"kind": "nli"}},
                           port=self.port, default_llm="llm")
        self.sup = Supervisor(self.cat, state_dir=self.d / "state", reap_interval=0.05, health_interval=0.05, grace_s=0.5)
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

    def img(self, url):
        return {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": "what is this?"}]}

    async def test_openai_chat_with_a_vision_model_inlines_local_paths(self):
        r = await self.http.post("/v1/chat/completions", json={"model": "vision", "messages": [self.img(str(self.d / "stills" / "s1.png"))]})
        self.assertEqual((r.status_code, r.headers["x-gateway-backend"]), (200, "eyes"))
        self.assertEqual(json.loads(r.json()["choices"][0]["message"]["content"])["images"], ["data:image/png;base64,iVBORw0KGgoAAAAAAAAAAA"[:40]])
        self.assertEqual(self.sup.rt["llm"].starts, 0)

    async def test_stream_request_gets_one_sse_chunk(self):
        r = await self.http.post("/v1/chat/completions", json={"model": "eyes", "stream": True, "messages": [self.img("data:image/png;base64,AAAA")]})
        self.assertEqual((r.status_code, r.headers["content-type"].split(";")[0]), (200, "text/event-stream"))
        events = [l[6:] for l in r.text.split("\n") if l.startswith("data: ")]
        self.assertEqual(events[-1], "[DONE]")
        self.assertIn('"ok": true', json.loads(events[0])["choices"][0]["delta"]["content"])

    async def test_images_to_a_text_model_and_urls_are_refused(self):
        r = await self.http.post("/v1/chat/completions", json={"model": "llm", "messages": [self.img("data:image/png;base64,AAAA")]})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "model_not_vision"))
        self.assertIn("eyes", r.json()["error"]["message"])
        r = await self.http.post("/v1/chat/completions", json={"model": "eyes", "messages": [self.img("https://example.com/a.png")]})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_arguments"))
        self.assertEqual(self.sup.rt["eyes"].starts, 0)                            # refused before any model started

    async def test_look_each_over_a_directory_with_the_layout_preset(self):
        r = await self.http.post("/api/look", json={"images": [str(self.d / "stills"), f"{self.d}/paper.pdf#page=2"], "preset": "layout"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual([x["image"] for x in d["results"]], [str(self.d / "stills" / "s1.png"), str(self.d / "stills" / "s2.png"), f"{self.d}/paper.pdf#page=2"])
        self.assertEqual(d["flagged"], [f"{self.d}/paper.pdf#page=2"])              # the fake says ok: false for a PDF page
        self.assertTrue(all(x["passed"] for x in d["results"]))                      # the preset's JSON-schema gate
        self.assertEqual(d["results"][0]["json"]["image_tokens"], 1120)              # the preset's detail
        self.assertEqual(d["model"], "eyes")

    async def test_look_retries_on_a_failing_gate_and_reports_it(self):
        r = await self.http.post("/api/look", json={"images": [str(self.d / "stills" / "s1.png")], "prompt": "FAIL please",
                                                    "gates": [{"type": "json"}], "max_attempts": 2})
        d = r.json()
        self.assertEqual((d["passed"], len(d["attempts"])), (False, 2))
        r = await self.http.post("/api/look", json={"images": [str(self.d / "stills" / "s1.png")], "prompt": "hi", "image_tokens": 280})
        self.assertEqual((r.json()["json"]["image_tokens"], r.json()["images"]), (280, [str(self.d / "stills" / "s1.png")]))

    async def test_look_validation(self):
        for body in ({"images": [], "prompt": "x"}, {"images": ["/nope.png"], "prompt": "x"}, {"images": ["/a.png"]},
                     {"images": ["/a.png"], "prompt": "x", "preset": "layout"}, {"images": ["/a.png"], "preset": "nope"},
                     {"images": "/a.png", "prompt": "x"}, {"images": ["/a.png"], "prompt": "x", "bogus": 1},
                     {"images": [str(self.d / "stills" / "s1.png")], "prompt": "x", "gates": [{"type": "shell"}]}):
            r = await self.http.post("/api/look", json=body)
            self.assertEqual(r.status_code, 400, body)
        self.assertEqual(self.sup.rt["eyes"].starts, 0)

    async def test_models_presets_and_page(self):
        ids = {m["id"]: m for m in (await self.http.get("/v1/models")).json()["data"]}
        self.assertTrue(ids["eyes"]["vision"])
        self.assertNotIn("vision", ids["llm"])
        p = (await self.http.get("/api/vision/presets")).json()
        self.assertEqual([m["name"] for m in p["models"]], ["eyes"])
        self.assertIn("layout", p["presets"])
        r = await self.http.get("/vision")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Content-Security-Policy", r.headers)


if __name__ == "__main__":
    unittest.main()
