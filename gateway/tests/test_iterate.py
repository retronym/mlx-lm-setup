"""Gates and the iterate loop (pure), plus the MCP tool end to end against fake backends."""
import json, unittest

from gateway.gates import GateSpecError, extract_json, parse_gates, run_gate, split_claims
from gateway.iterate import run_iterate


async def ok(spec, text, entail=None):
    return await run_gate(parse_gates([spec])[0], text, entail)


class GateTests(unittest.IsolatedAsyncioTestCase):
    def test_spec_validation(self):
        bad = [[], [{"type": "shell"}], [{"type": "regex", "pattern": "("}], [{"type": "regex", "pattern": "a", "x": 1}],
               [{"type": "contains"}], [{"type": "length"}], [{"type": "length", "min_chars": -1}],
               [{"type": "json", "schema": {"type": "nope"}}], [{"type": "nli"}], [{"type": "nli", "source": "s", "min_entailment": 2}]]
        for b in bad:
            with self.assertRaises(GateSpecError, msg=b):
                parse_gates(b)

    def test_extract_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json('Sure!\n```json\n{"a": 2}\n```\nDone'), {"a": 2})
        self.assertEqual(extract_json('The answer is {"a": [1, 2]} ok'), {"a": [1, 2]})
        with self.assertRaises(ValueError):
            extract_json("no json {here")

    async def test_json_and_schema(self):
        schema = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}
        self.assertTrue((await ok({"type": "json", "schema": schema}, '```json\n{"n": 3}\n```'))[0])
        good, msg = await ok({"type": "json", "schema": schema}, '{"n": "x"}')
        self.assertFalse(good)
        self.assertIn("n:", msg)
        self.assertFalse((await ok({"type": "json"}, "plain text"))[0])

    async def test_regex_and_contains_and_length(self):
        self.assertTrue((await ok({"type": "regex", "pattern": r"^def \w+\("}, "x\ndef f(a):"))[0])      # multiline
        self.assertFalse((await ok({"type": "regex", "pattern": "TODO", "mode": "absent"}, "a TODO b"))[0])
        self.assertTrue((await ok({"type": "regex", "pattern": "todo", "ignore_case": True}, "a TODO b"))[0])
        self.assertTrue((await ok({"type": "contains", "all": ["a", "b"], "none": ["z"]}, "ab"))[0])
        good, msg = await ok({"type": "contains", "all": ["a", "q"], "none": ["b"]}, "ab")
        self.assertFalse(good)
        self.assertIn("'q'", msg)
        self.assertIn("'b'", msg)
        self.assertFalse((await ok({"type": "contains", "any": ["x", "y"]}, "ab"))[0])
        self.assertTrue((await ok({"type": "contains", "any": ["X"], "ignore_case": True}, "x"))[0])
        self.assertFalse((await ok({"type": "length", "min_chars": 5}, " ab "))[0])
        self.assertFalse((await ok({"type": "length", "max_chars": 2}, "abc"))[0])

    async def test_catastrophic_regex_is_killed(self):
        good, msg = await ok({"type": "regex", "pattern": r"(a+)+$"}, "a" * 40 + "b")
        self.assertFalse(good)
        self.assertIn("timed out", msg)

    def test_claims(self):
        text = "Intro.\n- The cat sat on the mat. It was happy today.\n```py\nx = 1\n```\nOk."
        self.assertEqual(split_claims(text), ["The cat sat on the mat.", "It was happy today."])

    async def test_nli_gate(self):
        async def entail(premise, hyps):
            return [{"entailment": 0.9, "contradiction": 0.0} if "cat" in h else
                    {"entailment": 0.0, "contradiction": 0.9} if "dog" in h else {"entailment": 0.1, "contradiction": 0.1} for h in hyps]
        spec = {"type": "nli", "source": "a cat sat"}
        self.assertTrue((await ok(spec, "The cat sat down.", entail))[0])
        good, msg = await ok(spec, "The cat sat down. The dog barked loudly. Rain fell hard.", entail)
        self.assertFalse(good)
        self.assertIn("contradicted", msg)
        self.assertIn("not supported", msg)


class LoopTests(unittest.IsolatedAsyncioTestCase):
    def make_chat(self, replies):
        calls = []
        async def chat(model, convo):
            calls.append((model, convo))
            return replies[min(len(calls), len(replies)) - 1], {"model": model or "llm", "secs": 0.1}
        return chat, calls

    async def test_retries_with_feedback_until_pass(self):
        chat, calls = self.make_chat(["nope", '{"a": 1}'])
        r = await run_iterate(chat, None, parse_gates([{"type": "json"}]), [{"role": "user", "content": "q"}])
        self.assertTrue(r["passed"])
        self.assertEqual([a["passed"] for a in r["attempts"]], [False, True])
        convo = calls[1][1]
        self.assertEqual([m["role"] for m in convo], ["user", "assistant", "user"])
        self.assertIn("not valid JSON", convo[2]["content"])

    async def test_gives_up_and_returns_last_try(self):
        chat, calls = self.make_chat(["x"])
        r = await run_iterate(chat, None, parse_gates([{"type": "length", "min_chars": 5}]), [{"role": "user", "content": "q"}], max_attempts=2)
        self.assertFalse(r["passed"])
        self.assertEqual((len(r["attempts"]), r["text"]), (2, "x"))
        self.assertIn("note", r)
        self.assertEqual(len(calls[1][1]), 3)                          # history does not grow beyond the last try

    async def test_escalates_on_the_last_attempt_only(self):
        chat, calls = self.make_chat(["x", "x", "long enough"])
        r = await run_iterate(chat, None, parse_gates([{"type": "length", "min_chars": 5}]), [{"role": "user", "content": "q"}],
                              max_attempts=3, escalate_to="big")
        self.assertEqual([c[0] for c in calls], [None, None, "big"])
        self.assertTrue(r["passed"])

    async def test_nli_runs_only_after_cheap_gates_pass(self):
        seen = []
        async def entail(premise, hyps):
            seen.append(hyps)
            return [{"entailment": 0.9, "contradiction": 0.0} for _ in hyps]
        chat, _ = self.make_chat(["short", "The long answer is here today."])
        gates = parse_gates([{"type": "nli", "source": "s"}, {"type": "length", "min_chars": 10}])
        r = await run_iterate(chat, entail, gates, [{"role": "user", "content": "q"}])
        self.assertTrue(r["passed"])
        self.assertEqual(len(seen), 1)                                  # not consulted for the first, too-short answer
        self.assertEqual(r["attempts"][0]["failures"], ["the answer is too short (5 chars, need at least 10)"])


if __name__ == "__main__":
    unittest.main()
