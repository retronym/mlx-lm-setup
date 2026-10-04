import tempfile, unittest
from pathlib import Path
from unittest import mock

import httpx

from gateway import discovery, modelinfo
from gateway.app import create_app
from gateway.catalog import parse
from gateway.supervisor import Supervisor
from gateway.tests.test_discovery import FLAT, HYBRID, make_model

RAW = {
    "gateway": {"memory_budget_gb": 40, "port": 18998, "default_llm": "big"},
    "backends": {
        "big": {"adapter": "mlx_lm", "python": "/py", "model": "acme/hybrid-4bit", "aliases": ["b"], "description": "A big one.",
                "weights_gb": 20.4, "kv_gb": 0.7, "overhead_gb": 1.0, "context_tokens": 32768,
                "args": ["--chat-template-args", '{"enable_thinking": false}']},
        "plain": {"adapter": "mlx_lm", "python": "/py", "model": "acme/flat-4bit", "est_mem_gb": 18},
        "dec": {"adapter": "jevstyle", "python": "/py", "model_dir": "/m", "est_mem_gb": 3, "description": "decider"},
    },
    "profiles": {
        "big-think": {"backend": "big", "description": "reasoning on", "aliases": ["think"], "system_prompt": "Be careful.",
                      "defaults": {"max_tokens": 4096, "chat_template_kwargs": {"enable_thinking": True}}},
        "dec-warm": {"backend": "dec", "defaults": {"temperature": 2.0}},
    },
}


class ThinkingDefaultTests(unittest.TestCase):
    def setUp(self):
        self.cat = parse(RAW, Path("/base"))

    def test_from_server_args_and_profile(self):
        big, plain = self.cat.backends["big"], self.cat.backends["plain"]
        self.assertEqual(modelinfo.thinking_default(big), "off")                       # --chat-template-args {"enable_thinking": false}
        self.assertIsNone(modelinfo.thinking_default(plain))                           # nothing set: the model's own default
        self.assertEqual(modelinfo.thinking_default(big, self.cat.profiles["big-think"]), "on")   # the profile wins over the backend

    def test_equals_form_bad_json_and_non_bool(self):
        mk = lambda args: parse({**RAW, "backends": {"x": {**RAW["backends"]["big"], "args": args}}, "profiles": {}, "gateway": {"memory_budget_gb": 40}}, Path("/b")).backends["x"]
        self.assertEqual(modelinfo.thinking_default(mk(['--chat-template-args={"enable_thinking": true}'])), "on")
        self.assertIsNone(modelinfo.thinking_default(mk(["--chat-template-args", "{nope"])))
        self.assertIsNone(modelinfo.thinking_default(mk(["--chat-template-args", '{"enable_thinking": "yes"}'])))
        self.assertIsNone(modelinfo.thinking_default(mk(["--temp", "0.2"])))


class BuildEntriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "hub"
        make_model(root, "acme/hybrid-4bit", HYBRID, gb=20.4)
        make_model(root, "acme/flat-4bit", FLAT, gb=17.2)
        self.cat = parse(RAW, Path("/base"))
        found = discovery.scan(self.cat, [("hf-cache", root)], supported_types={"qwen3_5_moe", "qwen3_moe"})
        self.by = {f.id: f for f in found}
        self.snap = {"big": {"name": "big", "state": "ready", "busy": True, "idle_s": 12.3, "ttl_s": 600.0, "ttl_left_s": 587.7,
                             "pinned": False, "last_start_s": 1.2, "requests": 5}}

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self, **kw):
        return {e["id"]: e for e in modelinfo.build_entries(self.cat, self.snap, kw.get("by", self.by))}

    def test_only_llm_backends_and_llm_profiles_in_catalog_order(self):
        ids = [e["id"] for e in modelinfo.build_entries(self.cat, self.snap, self.by)]
        self.assertEqual(ids, ["big", "plain", "big-think"])                           # backends first, then profiles; no decision entries

    def test_backend_entry_describes_cost_and_behaviour(self):
        e = self.entries()["big"]
        self.assertEqual((e["type"], e["kind"], e["description"], e["model"]), ("backend", "llm", "A big one.", "acme/hybrid-4bit"))
        self.assertEqual(e["aliases"], ["b"])                                           # the HF id is `model`, not an alias
        self.assertEqual(e["memory"], {"est_gb": 22.1, "weights_gb": 20.4, "kv_gb": 0.7, "overhead_gb": 1.0, "context_tokens": 32768})
        self.assertEqual((e["thinking"], e["profiles"]), ("off", ["big-think"]))
        self.assertEqual(e["disk"]["kv_kib_per_token"], 20.0)                           # from discovery (hybrid: 10 full-attention layers)
        self.assertEqual(e["disk"]["max_context"], 262144)
        self.assertEqual((e["state"], e["ready"], e["busy"], e["ttl_left_s"], e["last_start_s"]), ("ready", True, True, 587.7, 1.2))

    def test_plain_estimate_has_no_parts_and_stopped_state(self):
        e = self.entries()["plain"]
        self.assertEqual(e["memory"], {"est_gb": 18.0, "weights_gb": None, "kv_gb": None, "overhead_gb": None, "context_tokens": None})
        self.assertEqual((e["state"], e["ready"], e["busy"], e["idle_s"], e["requests"]), ("stopped", False, False, None, 0))
        self.assertIsNone(e["thinking"])
        self.assertEqual(e["disk"]["kv_kib_per_token"], 96.0)

    def test_profile_entry_shares_the_backend_and_shows_its_defaults(self):
        e = self.entries()["big-think"]
        self.assertEqual((e["type"], e["backend"], e["thinking"], e["system_prompt"], e["aliases"]), ("profile", "big", "on", True, ["think"]))
        self.assertEqual(e["defaults"], {"max_tokens": 4096, "chat_template_kwargs": {"enable_thinking": True}})
        self.assertEqual(e["memory"]["est_gb"], 22.1)                                   # same process, so the backend's memory
        self.assertEqual((e["state"], e["ready"]), ("ready", True))                     # live state is the backend's
        self.assertEqual(e["profiles"], [])

    def test_works_without_discovery(self):
        e = self.entries(by={})["big"]
        self.assertIsNone(e["disk"])
        self.assertEqual(e["memory"]["est_gb"], 22.1)


class ModelsEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "hub"
        make_model(root, "acme/hybrid-4bit", HYBRID, gb=20.4)
        self.cat = parse(RAW, Path(self.tmp.name))
        self.patches = [mock.patch.object(discovery, "default_dirs", lambda: [("hf-cache", root)]),
                        mock.patch.object(discovery, "mlx_lm_supported_types", lambda py: {"qwen3_5_moe"})]
        for p in self.patches:
            p.start()
        self.sup = Supervisor(self.cat, state_dir=Path(self.tmp.name) / "state")                    # the lifespan would create this; ASGITransport has none
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.cat, supervisor=self.sup)), base_url="http://127.0.0.1:18998")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.sup.shutdown()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def test_api_models_returns_rich_entries_without_starting_anything(self):
        r = await self.http.get("/api/models")
        self.assertEqual(r.status_code, 200)
        by = {m["id"]: m for m in r.json()["models"]}
        self.assertEqual(set(by), {"big", "plain", "big-think"})
        self.assertEqual(by["big"]["disk"]["kv_kib_per_token"], 20.0)
        self.assertEqual(by["big-think"]["thinking"], "on")
        self.assertEqual({m["state"] for m in by.values()}, {"stopped"})                # read-only: nothing was started

    async def test_discovery_failure_never_breaks_the_picker(self):
        with mock.patch.object(discovery, "scan", side_effect=RuntimeError("boom")):
            r = await self.http.get("/api/models")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone({m["id"]: m for m in r.json()["models"]}["big"]["disk"])

    async def test_v1_models_stays_lean_and_compatible(self):
        data = (await self.http.get("/v1/models")).json()["data"]
        self.assertTrue({"id", "object", "owned_by", "state", "ready"} <= set(data[0]))
        self.assertNotIn("memory", data[0])                                             # the rich fields live on /api/models

    async def test_host_policy_applies(self):
        r = await self.http.get("/api/models", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 421)

    async def test_post_is_not_served(self):
        r = await self.http.post("/api/models", json={})
        self.assertIn(r.status_code, (404, 405))


if __name__ == "__main__":
    unittest.main()
