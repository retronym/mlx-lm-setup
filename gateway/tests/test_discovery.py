import json, os, struct, tempfile, tomllib, unittest
from pathlib import Path
from unittest import mock

import httpx

from gateway import discovery, kv
from gateway.app import create_app
from gateway.catalog import parse

FLAT = {"model_type": "qwen3_moe", "architectures": ["Qwen3MoeForCausalLM"], "num_hidden_layers": 48, "num_key_value_heads": 4,
        "head_dim": 128, "hidden_size": 2048, "num_attention_heads": 32, "max_position_embeddings": 262144,
        "quantization": {"group_size": 64, "bits": 4}}
HYBRID = {"model_type": "qwen3_5_moe", "architectures": ["Qwen3_5MoeForConditionalGeneration"], "quantization": {"group_size": 64, "bits": 4},
          "text_config": {"num_hidden_layers": 40, "num_key_value_heads": 2, "head_dim": 256, "hidden_size": 2048, "num_attention_heads": 16,
                          "layer_types": (["linear_attention"] * 3 + ["full_attention"]) * 10, "max_position_embeddings": 262144}}
GEMMA = {"model_type": "gemma4", "architectures": ["Gemma4ForConditionalGeneration"], "quantization": {"group_size": 64, "bits": 4},
         "text_config": {"num_hidden_layers": 30, "num_key_value_heads": 8, "head_dim": 256, "num_global_key_value_heads": 2,
                         "global_head_dim": 512, "sliding_window": 1024, "layer_types": (["sliding_attention"] * 5 + ["full_attention"]) * 5}}
MLA = {"model_type": "deepseek_v3", "architectures": ["DeepseekV3ForCausalLM"], "num_hidden_layers": 10, "kv_lora_rank": 512,
       "qk_rope_head_dim": 64, "num_key_value_heads": 128, "head_dim": 128, "quantization": {"group_size": 64, "bits": 4}}


class KVTests(unittest.TestCase):
    def test_flat_full_attention_is_layers_x_heads_x_dim(self):
        self.assertEqual(kv.kv_bytes_per_token(FLAT), 48 * 2 * 4 * 128 * 2)               # 98,304 B = 96 KiB per token
        self.assertAlmostEqual(kv.kv_gb(FLAT, 32768), 98304 * 32768 / 1e9, places=6)

    def test_hybrid_counts_only_full_attention_layers(self):
        self.assertEqual(kv.kv_bytes_per_token(HYBRID), 10 * 2 * 2 * 256 * 2)             # 20,480 B = 20 KiB per token
        plan = kv.layer_plan(HYBRID)
        self.assertEqual([p["kind"] for p in plan].count("linear"), 30)
        self.assertTrue(all(p["per_token"] == 0 for p in plan if p["kind"] == "linear"))

    def test_sliding_window_stops_growing_and_global_layers_use_their_own_dims(self):
        per_global = 5 * 2 * 2 * 512 * 2                                                  # 5 global layers
        self.assertEqual(kv.kv_bytes_per_token(GEMMA), per_global)                        # long-context slope = global layers only
        sliding_per_tok = 25 * 2 * 8 * 256 * 2
        self.assertEqual(kv.kv_bytes(GEMMA, 512), 512 * (per_global + sliding_per_tok))   # below the window both grow
        self.assertEqual(kv.kv_bytes(GEMMA, 4096), 4096 * per_global + 1024 * sliding_per_tok)   # above it sliding is capped
        self.assertEqual(kv.kv_bytes(GEMMA, 8192) - kv.kv_bytes(GEMMA, 4096), 4096 * per_global)

    def test_mla_uses_one_compressed_latent_per_layer(self):
        self.assertEqual(kv.kv_bytes_per_token(MLA), 10 * (512 + 64) * 2)

    def test_quantized_kv_includes_group_overhead(self):
        self.assertAlmostEqual(kv.bytes_per_element(4), 0.5 + 4 / 64)
        self.assertAlmostEqual(kv.kv_bytes(FLAT, 1000, 4) / kv.kv_bytes(FLAT, 1000), (0.5 + 4 / 64) / 2)
        self.assertEqual(kv.bytes_per_element(None), 2.0)
        with self.assertRaises(kv.KVEstimateError):
            kv.bytes_per_element(7)

    def test_derived_layer_types_and_errors(self):
        cfg = {"num_hidden_layers": 8, "full_attention_interval": 4, "num_key_value_heads": 2, "head_dim": 64}
        self.assertEqual([p["kind"] for p in kv.layer_plan(cfg)], ["linear"] * 3 + ["full"] + ["linear"] * 3 + ["full"])
        with self.assertRaises(kv.KVEstimateError):
            kv.layer_plan({"model_type": "x"})                                           # no layer count
        with self.assertRaises(kv.KVEstimateError):
            kv.layer_plan({"num_hidden_layers": 4, "layer_types": ["full_attention"]})     # length mismatch
        with self.assertRaises(kv.KVEstimateError):
            kv.layer_plan({"num_hidden_layers": 2})                                      # no head info at all
        self.assertEqual(kv.max_context(FLAT), 262144)
        self.assertIsNone(kv.max_context(GEMMA))


def make_model(root: Path, mid: str, cfg: dict, *, mlx=True, gb=1.0, snap="abc", source="hf-cache", shards=1):
    """A synthetic model directory with sparse safetensors files (no real weights)."""
    if source == "hf-cache":
        d = root / ("models--" + mid.replace("/", "--")) / "snapshots" / snap
    else:
        d = root / mid
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(cfg))
    for i in range(shards):
        header = {"__metadata__": {"format": "mlx"} if mlx else {}, "w": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}}
        h = json.dumps(header).encode()
        f = d / f"model-{i:05d}.safetensors"
        f.write_bytes(struct.pack("<Q", len(h)) + h + b"\0\0")
        os.truncate(f, int(gb * 1e9 / shards))                                           # sparse: reports the size, uses no disk
    return d


CATALOG = {"gateway": {"memory_budget_gb": 28}, "backends": {
    "have-it": {"adapter": "mlx_lm", "python": "/py", "model": "acme/flat-4bit", "est_mem_gb": 10}}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "hub"
        make_model(self.root, "acme/flat-4bit", FLAT, gb=17.2, shards=3)
        make_model(self.root, "acme/hybrid-4bit", HYBRID, gb=20.4)
        make_model(self.root, "acme/gemma-it-4bit", GEMMA, gb=15.6)
        make_model(self.root, "acme/notmlx", {k: v for k, v in FLAT.items() if k != "quantization"}, mlx=False, gb=5)                      # plain safetensors: ignored
        make_model(self.root, "acme/classifier", {**FLAT, "architectures": ["Qwen3ForSequenceClassification"]}, gb=4)
        self.cat = parse(CATALOG, Path("/base"))
        self.types = {"qwen3_moe", "qwen3_5_moe", "gemma4"}

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, **kw):
        return discovery.scan(self.cat, [("hf-cache", self.root)], supported_types=kw.pop("supported_types", self.types), **kw)

    def test_scan_finds_mlx_models_with_sizes_and_cross_references(self):
        found = {f.id: f for f in self.scan()}
        self.assertEqual(set(found), {"acme/flat-4bit", "acme/hybrid-4bit", "acme/gemma-it-4bit", "acme/classifier"})   # notmlx excluded
        f = found["acme/flat-4bit"]
        self.assertEqual((f.weights_gb, f.model_type, f.quant_bits, f.kind_guess, f.in_catalog), (17.2, "qwen3_moe", 4, "llm", ["have-it"]))
        self.assertEqual(f.kv_kib_per_token, 96.0)
        self.assertEqual(found["acme/hybrid-4bit"].kv_kib_per_token, 20.0)
        self.assertEqual(found["acme/hybrid-4bit"].in_catalog, [])
        self.assertEqual(found["acme/classifier"].kind_guess, "other")
        self.assertEqual({f.id for f in self.scan(llm_only=True)}, {"acme/flat-4bit", "acme/hybrid-4bit", "acme/gemma-it-4bit"})

    def test_support_flag_comes_from_the_installed_runtime(self):
        by = {f.id: f.supported for f in self.scan(supported_types={"qwen3_moe"})}
        self.assertEqual((by["acme/flat-4bit"], by["acme/hybrid-4bit"]), (True, False))

    def test_unreadable_kv_config_is_reported_not_fatal(self):
        make_model(self.root, "acme/odd-4bit", {"model_type": "odd", "architectures": ["OddForCausalLM"], "quantization": {"bits": 4, "group_size": 64}}, gb=2)
        f = next(f for f in self.scan() if f.id == "acme/odd-4bit")
        self.assertIsNone(f.kv_kib_per_token)
        self.assertIn("num_hidden_layers", f.kv_error)
        self.assertIsNone(f.kv_gb(1000))

    def test_refs_main_selects_the_snapshot_and_first_source_wins(self):
        d = self.root / "models--acme--flat-4bit"
        make_model(self.root, "acme/flat-4bit", {**FLAT, "model_type": "qwen3_moe_NEW"}, gb=1, snap="def")
        (d / "refs").mkdir()
        (d / "refs" / "main").write_text("abc")
        self.assertEqual(next(f for f in self.scan() if f.id == "acme/flat-4bit").model_type, "qwen3_moe")   # refs/main -> abc, not newest
        lm = Path(self.tmp.name) / "lms"
        make_model(lm, "acme/flat-4bit", {**FLAT, "model_type": "from_lmstudio"}, gb=9, source="lm-studio")
        found = discovery.scan(self.cat, [("hf-cache", self.root), ("lm-studio", lm)], supported_types=self.types)
        self.assertEqual(next(f for f in found if f.id == "acme/flat-4bit").source, "hf-cache")

    def test_lm_studio_layout(self):
        lm = Path(self.tmp.name) / "lms"
        make_model(lm, "pub/gemma-4bit", GEMMA, gb=3, source="lm-studio")
        found = discovery.scan(self.cat, [("lm-studio", lm)], supported_types=self.types)
        self.assertEqual([(f.id, f.source) for f in found], [("pub/gemma-4bit", "lm-studio")])

    def test_missing_directories_are_fine(self):
        self.assertEqual(discovery.scan(self.cat, [("hf-cache", Path("/nonexistent/x"))], supported_types=set()), [])

    def test_scan_is_read_only(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        self.scan()
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)

    # ---- snippet: must be valid catalog ----
    def snippet_catalog(self, text):
        data = tomllib.loads(text)                                                         # valid TOML on its own
        return parse({"gateway": {"memory_budget_gb": 100}, **data}, Path("/base"))

    def test_snippet_round_trips_through_the_catalog_parser(self):
        f = next(f for f in self.scan() if f.id == "acme/hybrid-4bit")
        c = self.snippet_catalog(discovery.snippet(f, context_tokens=32768, python="/py"))
        spec = c.backends["hybrid"]                                                        # "-4bit" suffix dropped from the name
        self.assertEqual((spec.weights_gb, spec.context_tokens, spec.overhead_gb), (20.4, 32768, 1.0))
        self.assertAlmostEqual(spec.est_mem_gb, 20.4 + 0.7 + 1.0, places=1)                # 20 KiB x 32768 = 0.67 GB -> 0.7
        self.assertEqual(spec.options["model"], "acme/hybrid-4bit")

    def test_snippet_with_quantized_kv_adds_the_flag_and_shrinks_kv(self):
        f = next(f for f in self.scan() if f.id == "acme/flat-4bit")
        fp16 = self.snippet_catalog(discovery.snippet(f, context_tokens=131072, python="/py")).backends["flat"]
        q4 = self.snippet_catalog(discovery.snippet(f, context_tokens=131072, kv_bits=4, python="/py")).backends["flat"]
        self.assertGreater(fp16.kv_gb, 3 * q4.kv_gb)                                       # 12.9 GB vs 3.6 GB
        self.assertEqual(q4.command()[-2:], ["--kv-bits", "4"])
        self.assertNotIn("--kv-bits", fp16.command())

    def test_snippet_warns_when_the_runtime_lacks_the_architecture(self):
        f = next(f for f in self.scan(supported_types={"qwen3_moe"}) if f.id == "acme/hybrid-4bit")
        text = discovery.snippet(f, python="/py")
        self.assertTrue(text.startswith("# WARNING"))
        self.snippet_catalog(text)                                                         # still valid TOML and catalog (comment only)

    def test_snippet_for_unestimable_model_asks_the_user_to_set_kv(self):
        make_model(self.root, "acme/odd-4bit", {"model_type": "odd", "architectures": ["OddForCausalLM"], "quantization": {"bits": 4, "group_size": 64}}, gb=2)
        f = next(f for f in self.scan() if f.id == "acme/odd-4bit")
        text = discovery.snippet(f, python="/py")
        self.assertIn("set kv_gb yourself", text)
        self.assertEqual(self.snippet_catalog(text).backends["odd"].kv_gb, 0.0)

    def test_slug(self):
        self.assertEqual(discovery._slug("mlx-community/Qwen3.6-35B-A3B-4bit"), "qwen3-6-35b-a3b")
        self.assertEqual(discovery._slug("lmstudio-community/gemma-4-26B-A4B-it-QAT-MLX-4bit"), "gemma-4-26b-a4b-it-qat")
        self.assertEqual(discovery._ceil1(17.21), 17.3)
        self.assertEqual(discovery._ceil1(20.4), 20.4)


class DiscoveryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "hub"
        make_model(self.root, "acme/flat-4bit", FLAT, gb=17.2)
        make_model(self.root, "acme/hybrid-4bit", HYBRID, gb=20.4)
        self.cat = parse({**CATALOG, "gateway": {"port": 18999, "memory_budget_gb": 28}}, Path(self.tmp.name))     # a writable base: the supervisor creates .gateway/
        self.patches = [mock.patch.object(discovery, "default_dirs", lambda: [("hf-cache", self.root)]),
                        mock.patch.object(discovery, "mlx_lm_supported_types", lambda py: {"qwen3_moe", "qwen3_5_moe"})]
        for p in self.patches:
            p.start()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.cat)), base_url="http://127.0.0.1:18999")

    async def asyncTearDown(self):
        await self.http.aclose()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def test_discovered_lists_models_with_kv_at_the_requested_context(self):
        r = await self.http.get("/api/models/discovered", params={"context_tokens": 131072, "kv_bits": 4})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual((j["context_tokens"], j["kv_bits"]), (131072, 4))
        by = {m["id"]: m for m in j["models"]}
        self.assertEqual(set(by), {"acme/flat-4bit", "acme/hybrid-4bit"})
        self.assertEqual(by["acme/flat-4bit"]["in_catalog"], ["have-it"])
        self.assertAlmostEqual(by["acme/flat-4bit"]["kv_gb_at_context"], 3.62, places=1)
        self.assertEqual(by["acme/hybrid-4bit"]["supported"], True)

    async def test_snippet_by_id_is_plain_text_and_parses(self):
        r = await self.http.get("/api/models/snippet", params={"id": "acme/hybrid-4bit", "context_tokens": 32768})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertIn("[backends.hybrid]", r.text)
        tomllib.loads(r.text)

    async def test_snippet_takes_ids_not_paths_and_validates_params(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "acme/nope", ""):
            r = await self.http.get("/api/models/snippet", params={"id": bad})
            self.assertEqual((r.status_code, r.json()["error"]["type"]), (404, "model_not_found"), bad)
        for params in ({"context_tokens": "abc"}, {"context_tokens": 10}, {"kv_bits": 7}, {"kv_bits": "x"}):
            r = await self.http.get("/api/models/discovered", params=params)
            self.assertEqual((r.status_code, r.json()["error"]["type"]), (400, "invalid_parameter"), params)

    async def test_read_only_methods(self):
        for path in ("/api/models/discovered", "/api/models/snippet"):
            for method in ("POST", "PUT", "DELETE"):
                r = await self.http.request(method, path, json={})
                self.assertIn(r.status_code, (404, 405), (method, path))        # no handler: 404 from the catch-all MCP mount or 405


if __name__ == "__main__":
    unittest.main()
