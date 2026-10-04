import copy, unittest
from pathlib import Path

from gateway.catalog import CatalogError, parse
from gateway.profiles import ProfileError, apply_defaults, parse_profiles

BASE = Path("/base")
OK = {
    "gateway": {"memory_budget_gb": 28, "default_llm": "a"},
    "backends": {
        "a": {"adapter": "mlx_lm", "python": "/py", "model": "org/model", "est_mem_gb": 10},
        "b": {"adapter": "jevstyle", "python": "venv/bin/python", "model_dir": "models/j", "est_mem_gb": 3},
        "c": {"adapter": "openjev_nli", "python": "venv/bin/python", "root": "models/o", "subfolder": "sf", "est_mem_gb": 9},
    },
    "profiles": {
        "think": {"backend": "a", "description": "reasoning on", "aliases": ["deep"], "system_prompt": "Be careful.",
                  "defaults": {"max_tokens": 4096, "temperature": 0.6, "chat_template_kwargs": {"enable_thinking": True, "x": 1}}},
        "fast": {"backend": "a", "defaults": {"chat_template_kwargs": {"enable_thinking": False}}},
        "calm": {"backend": "b", "defaults": {"temperature": 2.0}},
    },
}


def variant(**patch):
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


class ApplyDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.cat = parse(OK, BASE)
        self.think = self.cat.profiles["think"]

    def test_fills_missing_keys_only(self):
        out = apply_defaults({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, self.think)
        self.assertEqual((out["max_tokens"], out["temperature"]), (4096, 0.6))
        out = apply_defaults({"max_tokens": 50, "temperature": 0, "messages": []}, self.think)
        self.assertEqual((out["max_tokens"], out["temperature"]), (50, 0))             # client wins, even falsy values
        out = apply_defaults({"max_tokens": None, "messages": []}, self.think)
        self.assertEqual(out["max_tokens"], 4096)                                       # null counts as "not sent"

    def test_chat_template_kwargs_merge_key_by_key(self):
        out = apply_defaults({"chat_template_kwargs": {"enable_thinking": False}, "messages": []}, self.think)
        self.assertEqual(out["chat_template_kwargs"], {"enable_thinking": False, "x": 1})   # client flips one flag, keeps the rest
        out = apply_defaults({"messages": []}, self.think)
        self.assertEqual(out["chat_template_kwargs"], {"enable_thinking": True, "x": 1})

    def test_system_prompt_only_when_absent(self):
        out = apply_defaults({"messages": [{"role": "user", "content": "hi"}]}, self.think)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "Be careful."})
        own = [{"role": "system", "content": "mine"}, {"role": "user", "content": "hi"}]
        self.assertEqual(apply_defaults({"messages": own}, self.think)["messages"], own)

    def test_does_not_mutate_inputs(self):
        body = {"messages": [{"role": "user", "content": "hi"}], "chat_template_kwargs": {"a": 1}}
        snap = copy.deepcopy(body)
        out = apply_defaults(body, self.think)
        self.assertEqual(body, snap)
        out["chat_template_kwargs"]["zzz"] = 1
        self.assertNotIn("zzz", self.think.defaults["chat_template_kwargs"])           # profile defaults are not aliased into the output

    def test_no_profile_is_identity(self):
        body = {"a": 1}
        self.assertIs(apply_defaults(body, None), body)


class ParseProfilesTests(unittest.TestCase):
    def check(self, patch, frag):
        with self.assertRaises(CatalogError) as cm:
            parse(variant(**patch), BASE)
        self.assertIn(frag, str(cm.exception))

    def test_ok(self):
        c = parse(OK, BASE)
        self.assertEqual(set(c.profiles), {"think", "fast", "calm"})
        self.assertEqual(c.profiles["think"].aliases, ("deep",))

    def test_errors(self):
        self.check({"profiles__think__backend": "nope"}, "is not a backend")
        self.check({"profiles__think__surprise": 1}, "unknown keys ['surprise']")
        self.check({"profiles__think__defaults": {"nonsense": 1}}, "not allowed for llm")
        self.check({"profiles__think__defaults": {"temperature": "hot"}}, "expected int/float")
        self.check({"profiles__think__defaults": {"temperature": True}}, "expected int/float")      # bool is not a number
        self.check({"profiles__think__defaults": {"stop": [1]}}, "list of strings")
        self.check({"profiles__calm__defaults": {"max_tokens": 5}}, "not allowed for decision")
        self.check({"profiles__calm__system_prompt": "x"}, "only llm backends")
        self.check({"profiles__think__system_prompt": "  "}, "non-empty string")
        c = {"profiles": {"p": {"backend": "c", "defaults": {"temperature": 1}}}}
        with self.assertRaises(CatalogError):
            parse({**copy.deepcopy(OK), **c}, BASE)                                    # nli accepts no defaults

    def test_name_collisions(self):
        self.check({"profiles__org/model": {"backend": "a"}}, "use letters")            # not a legal name
        d = variant()
        d["profiles"]["a"] = {"backend": "a"}                                           # same as a backend name
        with self.assertRaises(CatalogError) as cm:
            parse(d, BASE)
        self.assertIn("collides with backend", str(cm.exception))
        d = variant()
        d["profiles"]["fast"]["aliases"] = ["deep"]                                     # alias already used by profile 'think'
        with self.assertRaises(CatalogError):
            parse(d, BASE)

    def test_resolve_and_find(self):
        c = parse(OK, BASE)
        spec, prof = c.resolve("deep", "llm")
        self.assertEqual((spec.name, prof.name), ("a", "think"))
        spec, prof = c.resolve("org/model", "llm")                                      # backend alias: no profile
        self.assertEqual((spec.name, prof), ("a", None))
        self.assertEqual(c.find("fast", "llm").name, "a")                               # find() accepts profile names too
        self.assertEqual(c.resolve("calm", "decision")[0].name, "b")
        with self.assertRaises(ValueError):
            c.resolve("calm", "llm")                                                    # profile of a decision backend, asked as llm
        with self.assertRaises(KeyError):
            c.resolve("nope", "llm")


class CatalogExtrasTests(unittest.TestCase):
    def check(self, patch, frag):
        with self.assertRaises(CatalogError) as cm:
            parse(variant(**patch), BASE)
        self.assertIn(frag, str(cm.exception))

    def test_args_passthrough_appended_to_mlx_lm_command(self):
        d = variant(**{"backends__a__args": ["--kv-bits", "4", "--chat-template-args", '{"enable_thinking": false}']})
        cmd = parse(d, BASE).backends["a"].command()
        self.assertEqual(cmd[-4:], ["--kv-bits", "4", "--chat-template-args", '{"enable_thinking": false}'])
        self.assertEqual(cmd[:3], ["/py", "-m", "mlx_lm.server"])

    def test_args_reserved_flags_and_types(self):
        for bad_arg in (["--port", "1"], ["--port=9"], ["--model", "x"], ["--host", "0.0.0.0"]):
            self.check({"backends__a__args": bad_arg}, "is set by the gateway")
        self.check({"backends__a__args": "--kv-bits 4"}, "list of strings")
        self.check({"backends__a__args": ["--x", 4]}, "list of strings")
        self.check({"backends__b__args": ["--x"]}, "unknown keys ['args']")             # only mlx_lm takes passthrough args

    def test_memory_parts_sum_and_defaults(self):
        d = variant(**{"backends__a__est_mem_gb": None, "backends__a__weights_gb": 17.0, "backends__a__kv_gb": 3.2,
                       "backends__a__overhead_gb": 0.8, "backends__a__context_tokens": 32768})
        a = parse(d, BASE).backends["a"]
        self.assertEqual((a.est_mem_gb, a.weights_gb, a.kv_gb, a.overhead_gb, a.context_tokens), (21.0, 17.0, 3.2, 0.8, 32768))
        d = variant(**{"backends__a__est_mem_gb": None, "backends__a__weights_gb": 17.0})
        a = parse(d, BASE).backends["a"]
        self.assertEqual((a.est_mem_gb, a.kv_gb, a.overhead_gb), (18.0, 0.0, 1.0))      # kv defaults 0, overhead defaults 1 GB
        self.assertIsNone(parse(OK, BASE).backends["a"].weights_gb)                     # plain est_mem_gb leaves the parts unset

    def test_memory_errors(self):
        self.check({"backends__a__weights_gb": 5.0}, "est_mem_gb OR the parts")         # both forms given
        self.check({"backends__a__est_mem_gb": None, "backends__a__kv_gb": 2.0}, "weights_gb is required")
        self.check({"backends__a__est_mem_gb": None, "backends__a__weights_gb": -1}, "weights_gb must be > 0")
        self.check({"backends__a__est_mem_gb": None, "backends__a__weights_gb": 5, "backends__a__kv_gb": -1}, "kv_gb must be >= 0")
        self.check({"backends__a__est_mem_gb": None, "backends__a__weights_gb": 30.0}, "exceeds the whole memory budget")
        self.check({"backends__a__est_mem_gb": None}, "est_mem_gb is required")
        self.check({"backends__a__context_tokens": 0}, "context_tokens must be > 0")


if __name__ == "__main__":
    unittest.main()
