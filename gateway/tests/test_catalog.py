import tempfile, unittest
from pathlib import Path
from gateway.catalog import CatalogError, load, parse

BASE = Path("/base")
OK = {
    "gateway": {"memory_budget_gb": 28, "default_llm": "a"},
    "backends": {
        "a": {"adapter": "mlx_lm", "python": "/py/bin/python", "model": "org/model", "est_mem_gb": 10, "ttl_s": 60},
        "b": {"adapter": "jevstyle", "python": "venv/bin/python", "model_dir": "models/j", "est_mem_gb": 3},
        "c": {"adapter": "openjev_nli", "python": "venv/bin/python", "root": "models/o", "subfolder": "sf", "est_mem_gb": 9},
        "d": {"adapter": "command", "command": ["serve", "--port", "{port}"], "kind": "llm", "est_mem_gb": 1, "pinned": True},
    },
}


def bad(**patch):
    import copy
    d = copy.deepcopy(OK)
    for path, v in patch.items():
        cur = d
        *ks, last = path.split("__")
        for k in ks:
            cur = cur[k]
        if v is None:
            cur.pop(last, None)
        else:
            cur[last] = v
    return d


class CatalogTests(unittest.TestCase):
    def test_ok_and_ports_and_commands(self):
        c = parse(OK, BASE)
        self.assertEqual([s.port for s in c.backends.values()], [18101, 18102, 18103, 18104])
        a, b, _, d = c.backends.values()
        self.assertEqual(a.command(), ["/py/bin/python", "-m", "mlx_lm.server", "--model", "org/model", "--host", "127.0.0.1", "--port", "18101"])
        self.assertEqual(b.python, "/base/venv/bin/python")                       # relative paths resolve against the catalog dir
        self.assertIn("/base/models/j", b.command())
        self.assertEqual(d.command(), ["serve", "--port", "18104"])               # {port} substituted
        self.assertTrue(d.pinned)
        self.assertEqual((a.kind, b.kind, d.kind), ("llm", "decision", "llm"))

    def test_adding_a_model_is_config_only(self):
        d = bad()
        d["backends"]["gpt-oss"] = {"adapter": "mlx_lm", "python": "/py", "model": "x/y", "est_mem_gb": 13}
        self.assertIn("gpt-oss", parse(d, BASE).backends)

    def test_errors(self):
        for patch, frag in [
            ({"backends__a__adapter": "nope"}, "is not one of"),
            ({"backends__a__model": None}, "missing required keys ['model']"),
            ({"backends__a__est_mem_gb": None}, "est_mem_gb is required"),
            ({"backends__a__est_mem_gb": "10"}, "expected int/float"),
            ({"backends__a__est_mem_gb": 99}, "exceeds the whole memory budget"),
            ({"backends__a__ttl_s": -1}, "ttl_s must be >= 0"),
            ({"backends__a__surprise": 1}, "unknown keys ['surprise']"),
            ({"backends__d__command": []}, "non-empty list"),
            ({"gateway__host": "0.0.0.0"}, "loopback"),
            ({"gateway__default_llm": "b"}, "must name an llm backend"),
            ({"gateway__typo": 1}, "unknown keys"),
            ({"backends__a__start_timeout_s": 0}, "start_timeout_s must be > 0"),
            ({"backends__a__concurrency": 0}, "concurrency must be >= 1"),
        ]:
            with self.subTest(patch=patch):
                with self.assertRaises(CatalogError) as cm:
                    parse(bad(**patch), BASE)
                self.assertIn(frag, str(cm.exception))

    def test_shipped_gateway_toml_is_valid(self):
        c = load(Path(__file__).resolve().parents[2] / "gateway.toml")
        self.assertEqual(set(c.backends), {"qwen3-coder", "openjev-4b", "jevstyle-2b"})
        self.assertEqual(c.gateway.port, 8090)

    def test_defaults_and_overrides(self):
        d = bad(**{"backends__a__start_timeout_s": 5, "backends__a__concurrency": 2})
        c = parse(d, BASE)
        a, b = list(c.backends.values())[:2]
        self.assertEqual((a.start_timeout_s, a.concurrency), (5, 2))
        self.assertEqual((b.start_timeout_s, b.concurrency), (180, 1))

    def test_invalid_toml(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "x.toml"; p.write_text("[gateway\n")
            with self.assertRaises(CatalogError):
                load(p)


if __name__ == "__main__":
    unittest.main()
