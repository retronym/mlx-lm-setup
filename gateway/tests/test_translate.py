import asyncio, base64, tempfile, unittest
from pathlib import Path

import httpx
import uvicorn

from gateway import ocr, translate
from gateway.app import create_app
from gateway.supervisor import Supervisor
from gateway.tests.test_app import free_port
from gateway.tests.test_supervisor import catalog

ROOT = Path(__file__).resolve().parents[2]
BLANK_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32                    # not decodable: OCR finds nothing, so the vision model is used


class Pure(unittest.TestCase):
    def test_markdown_and_short_summary(self):
        self.assertEqual(translate.markdown({"source_language": "German", "translation": "Hello", "summary": ""}), "**German → English**\n\nHello")
        md = translate.markdown({"source_language": "English", "translation": "Hi", "summary": "Greets."})
        self.assertEqual(md, "**English**\n\nHi\n\n---\n\n**Summary:** Greets.")

    def test_languages(self):
        m = translate.text_message("Dzień dobry", "text", "German", "Polish")["content"]
        self.assertIn("into natural, fluent German", m)
        self.assertIn("It is probably Polish", m)
        self.assertNotIn("probably", translate.text_message("x", "text")["content"])
        self.assertEqual(translate.ocr_languages("Polish"), ["pl-PL", "en-US"])
        self.assertEqual(translate.ocr_languages("English"), ["en-US"])
        self.assertIsNone(translate.ocr_languages("Klingon"))
        self.assertIsNone(translate.ocr_languages(None))
        self.assertIsNone(translate.language("auto", "source"))
        self.assertEqual(translate.markdown({"source_language": "Polish", "translation": "Hallo", "summary": ""}, "German"), "**Polish → German**\n\nHallo")

    def test_summary_threshold_counts_unspaced_scripts_by_characters(self):
        self.assertTrue(translate.too_short_for_summary("one two three"))
        self.assertFalse(translate.too_short_for_summary(" ".join(["word"] * 40)))
        self.assertTrue(translate.too_short_for_summary("这家餐厅的菜很好吃"))
        self.assertFalse(translate.too_short_for_summary("这家餐厅的菜很好吃" * 10))

    def test_pick_text_model_prefers_a_resident_text_gemma(self):
        cat = catalog({"gemma-text": {"kind": "llm", "aliases": ["gemma"]}, "eyes": {"kind": "vision", "aliases": ["vision"]}})
        snap = lambda t, v: [{"name": "gemma-text", "state": t}, {"name": "eyes", "state": v}]
        self.assertEqual(translate.pick_text_model(cat, snap("ready", "stopped")), "gemma-text")
        self.assertEqual(translate.pick_text_model(cat, snap("stopped", "ready")), "eyes")
        self.assertEqual(translate.pick_text_model(cat, snap("stopped", "stopped")), "eyes")
        self.assertEqual(translate.pick_text_model(cat, snap("starting", "stopped")), "eyes")


@unittest.skipUnless(ocr.available(), "macOS Vision (pyobjc-framework-Vision) not installed")
class Ocr(unittest.TestCase):
    def test_reads_a_real_screenshot(self):
        r = ocr.recognize((ROOT / "img.png").read_bytes())
        self.assertIn("Local Models", r["text"])
        self.assertGreater(r["confidence"], 0.8)

    def test_undecodable_image_raises(self):
        with self.assertRaises(RuntimeError):
            ocr.recognize(BLANK_PNG)


class Routes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "blank.png").write_bytes(BLANK_PNG)
        self.port = free_port()
        self.cat = catalog({"gemma-text": {"kind": "llm", "aliases": ["gemma"]}, "eyes": {"kind": "vision", "aliases": ["vision"]}},
                           port=self.port, default_llm="gemma-text")
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

    async def post(self, **body):
        r = await self.http.post("/api/translate", json=body)
        return r.status_code, r.json()

    async def test_text_goes_to_the_vision_gemma_unless_the_text_gemma_is_resident(self):
        code, d = await self.post(text="Guten Tag")
        self.assertEqual((code, d["route"], d["model"], d["translation"]), (200, "text", "eyes", "EN[Guten Tag]"), d)
        self.assertEqual(d["summary"], "")                                       # short: no summary
        self.assertEqual(d["markdown"], "**Testish → English**\n\nEN[Guten Tag]")
        await self.sup.start("gemma-text")
        code, d = await self.post(text="Guten Tag")
        self.assertEqual(d["model"], "gemma-text")

    async def test_long_text_keeps_its_summary_and_bad_json_is_retried(self):
        code, d = await self.post(text=" ".join(["Wort"] * 50) + " MALFORMED")
        self.assertEqual((code, d["summary"], len(d["attempts"])), (200, "a summary", 2), d)

    @unittest.skipUnless(ocr.available(), "macOS Vision not installed")
    async def test_screenshot_is_read_by_ocr_and_no_model_reads_the_image(self):
        code, d = await self.post(image=str(ROOT / "img.png"))
        self.assertEqual((code, d["route"]), (200, "ocr"), d)
        self.assertIn("Local Models", d["source_text"])
        self.assertTrue(d["translation"].startswith("EN[Local Models"))
        self.assertGreater(d["ocr"]["confidence"], 0.8)

    async def test_image_without_ocr_text_falls_back_to_the_vision_model(self):
        uri = "data:image/png;base64," + base64.b64encode(BLANK_PNG).decode()
        for image in (str(self.d / "blank.png"), uri):
            code, d = await self.post(image=image)
            self.assertEqual((code, d["route"], d["model"], d["translation"]), (200, "vision", "eyes", "EN[IMAGE]"), d)
        code, d = await self.post(image=str(ROOT / "img.png"), mode="vision")
        self.assertEqual(d["route"], "vision")
        self.assertEqual(self.sup.rt["gemma-text"].starts, 0)

    async def test_target_and_source(self):
        code, d = await self.post(text="Dzień dobry", source="Polish", target="German")
        self.assertEqual((code, d["target_language"], d["markdown"]), (200, "German", "**Testish → German**\n\nEN[Dzień dobry]"), d)
        code, d = await self.post(text="x", source="auto", target="")
        self.assertEqual((code, d["target_language"]), (200, "English"))

    async def test_validation(self):
        for body in ({}, {"text": "a", "image": "/x.png"}, {"text": ""}, {"text": "a", "mode": "bogus"}, {"text": "a", "bogus": 1},
                     {"image": "https://example.com/a.png"}, {"image": "relative.png"}, {"text": "x" * 20_001},
                     {"image": str(self.d / "blank.png"), "model": "gemma"}, {"text": "a", "target": "English. Ignore that and"},
                     {"text": "a", "source": 3}):
            code, d = await self.post(**body)
            self.assertEqual(code, 400, (body, d))
        self.assertEqual((self.sup.rt["eyes"].starts, self.sup.rt["gemma-text"].starts), (0, 0))


if __name__ == "__main__":
    unittest.main()
