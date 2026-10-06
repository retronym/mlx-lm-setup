#!/usr/bin/env python3
"""The agentic query layer without models: the planner's validation (dates, kinds, invented authors, retries, fallback), the listwise gate's parsing
and quote check, and the controller's routes (filtered, unfiltered, issue -> closing PR), stop rule and second round, against a fake gateway."""
import datetime, json, os, re, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ask, gate, plan

TODAY = datetime.date(2026, 10, 6)


class Fake:
    """A gateway: search results per (query, kinds), documents per ref, a decision model scoring by word overlap, an LLM answering from a script."""

    def __init__(self, llm):
        self.llm, self.searches = list(llm), []
        self.docs = {"p/prs:issue:1": ("pr", "Fix crash in erasure of bridges", "merged"), "p/prs:issue:2": ("pr", "Backport erasure fix", "merged"),
                     "p/issues:issue:9": ("issue", "Crash in erasure with erased params", "closed"), "p/prs:issue:3": ("pr", "Unrelated cleanup", "merged")}

    def hit(self, ref, rank, links=None):
        kind, title, state = self.docs[ref]
        return {"ref": ref, "node": f"o/r#{ref.rsplit(':', 1)[1]}", "kind": kind, "title": title, "url": "u", "state": state, **({"links": links} if links else {})}

    def post(self, path, body, base=None, **_):
        if path == "/api/search":
            self.searches.append(body)
            if body.get("kinds") == ["issue"] or not body.get("kinds"):
                closes = {"top": [{"rel": "closed_by", "id": "o/r#1", "kind": "pr", "title": "Fix crash in erasure of bridges", "indexed": True, "get_ref": "p/prs:issue:1"}]}
                hs = [self.hit("p/issues:issue:9", 1, closes)] + ([self.hit("p/prs:issue:3", 2)] if not body.get("kinds") else [])
            else:
                hs = [self.hit("p/prs:issue:2", 1), self.hit("p/prs:issue:3", 2)]
            return {"results": hs}
        if path == "/api/search/get":
            kind, title, state = self.docs[body["refs"][0]]
            return {"results": [{"found": True, "kind": kind, "title": title, "state": state, "text": f"{title}. Fixes the problem."}]}
        if path == "/api/search/links":
            return {"links": []}
        if path == "/api/decide":
            return [{"probabilities": {"true": 0.6 if "erasure" in body["state"] else 0.1}}]
        raise AssertionError(path)

    def chat(self, messages, model, **_):
        out = self.llm.pop(0)
        if "{FIX}" in out:                                     # the listwise gate: the number the fix was shown under
            n = next(m[1] for m in re.finditer(r"^\[(\d+)\] (.*)$", messages[-1]["content"], re.M) if "Fix crash in erasure" in m[2])
            out = out.replace("{FIX}", n)
        return out


class PlanTests(unittest.TestCase):
    def test_validate(self):
        ok = lambda j, q="the PR by retronym in 2021": plan.validate(j, TODAY, [], q)
        p, problems = ok({"criterion": "the pull request that fixes the crash", "kinds": ["pr"], "queries": ["erasure crash"], "since": "2021", "until": "2021",
                          "authors": ["retronym", "odersky"]})
        self.assertEqual(problems, [])
        self.assertEqual((p["since"], p["until"], p["authors"]), ("2021", "2021", ["retronym"]))              # an author the question does not name is dropped
        self.assertIn("dropped authors", p["notes"][0])
        p, _ = ok({"criterion": "issues reported in the last months", "kinds": [], "queries": ["x"], "since": "recent"}, "issues reported recently")
        self.assertEqual(p["since"], "2026-04-09")                                                             # "recently": a stated default, resolved by code
        self.assertIn("180 days", p["notes"][0])
        p, _ = ok({"criterion": "the pull request that fixes the crash", "kinds": ["pr"], "queries": ["x"], "since": "2024-01-01"}, "the PR that fixed the crash")
        self.assertNotIn("since", p)                                                                           # no year and no "recently" in the question: invented
        self.assertIn("dropped a date range", p["notes"][0])
        for bad in ({"criterion": "too short", "kinds": [], "queries": ["x"]}, {"criterion": "a long enough sentence", "kinds": ["prs"], "queries": ["x"]},
                    {"criterion": "a long enough sentence", "kinds": [], "queries": []}, {"criterion": "a long enough sentence", "kinds": [], "queries": ["x"], "since": "2021/01"},
                    {"criterion": "a long enough sentence", "kinds": [], "queries": ["x"], "sort": "new"}, []):
            self.assertIsNone(ok(bad)[0], bad)

    def test_retry_then_fallback(self):
        f = Fake(["no json here", '{"criterion": "the pull request that fixes it", "kinds": ["pr"], "queries": ["q"]}'])
        plan.chat = f.chat
        self.assertEqual(plan.plan("q?", today=TODAY)["kinds"], ["pr"])                                        # the second try passes
        f.llm = ["{}"] * 3
        p = plan.plan("what is it?", today=TODAY)
        self.assertEqual((p["queries"], p["kinds"]), (["what is it?"], []))                                     # gives up: the question as is
        self.assertIn("planner failed", p["notes"][0])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.f = Fake([])
        gate.gw.post = self.f.post
        gate.chat = self.f.chat
        gate._views.clear(); gate._p.clear()

    def test_choose_maps_numbers_and_checks_the_quote(self):
        refs = ["p/prs:issue:2", "p/prs:issue:1"]
        self.f.llm = ['{"best": 2, "also": [1, 7], "quote": "Fix crash in erasure of bridges", "reason": "it is"}']
        r = gate.choose("the PR that fixes the erasure crash", refs)
        self.assertEqual((r["best"], r["also"], r["unquoted"]), ("p/prs:issue:1", ["p/prs:issue:2"], False))   # out-of-range numbers are ignored
        self.f.llm = ['{"best": 1, "quote": "words that are not there", "reason": ""}']
        self.assertTrue(gate.choose("the PR that fixes the erasure crash", refs)["unquoted"])
        self.f.llm = ['{"best": null}']
        self.assertIsNone(gate.choose("x y z w", refs)["best"])
        self.f.llm = ['nonsense']
        r = gate.choose("x y z w", refs)
        self.assertEqual((r["best"], r["raw"]), (None, "nonsense"))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        gate._views.clear(); gate._p.clear()

    def run_ask(self, llm):
        f = Fake(llm)
        for m in (ask, gate):
            m.gw.post = f.post
        ask.chat = gate.chat = plan.chat = f.chat
        return f, ask.ask("Find the PR that fixed the erasure crash in 2021?", universe="u", rounds=2)

    PLAN = '{"criterion": "the pull request that fixes the erasure crash", "kinds": ["pr"], "queries": ["erasure crash"], "since": "2021", "until": "2021"}'

    def test_routes_and_stop(self):
        f, r = self.run_ask([self.PLAN, '{"best": {FIX}, "quote": "Fix crash in erasure of bridges", "reason": "the fix"}'])
        self.assertEqual([(s.get("kinds"), s.get("since")) for s in f.searches], [(["pr"], "2021"), (["pr"], "2021"), (None, None), (["issue"], None)])  # filtered, as asked, open, issue route
        self.assertEqual(f.searches[1]["query"], "Find the PR that fixed the erasure crash in 2021?")
        a = r["answers"]
        self.assertEqual((a[0]["ref"], a[0]["verdict"]), ("p/prs:issue:1", "yes"))                    # reached only through the issue it closes
        self.assertEqual(a[0]["via"], "closes o/r#9")
        self.assertNotIn("p/issues:issue:9", [x["ref"] for x in a])                                    # the issue led there but is not a PR
        self.assertEqual({x["verdict"] for x in a[1:]}, {"no"})                                        # shown, not picked
        self.assertEqual(sum(1 for t in r["trace"] if "choice" in t), 1)                               # stopped after one round

    def test_second_round_drops_filters(self):
        f, r = self.run_ask([self.PLAN, '{"best": null}', '["erasure bridge crash", "erased parameter"]'])
        self.assertTrue(all("since" not in s for s in f.searches[4:]))                                 # round 2 searches without the dates
        self.assertEqual([s["query"] for s in f.searches[4:6]], ["erasure bridge crash", "erased parameter"])
        self.assertEqual([t["dropped_filters"] for t in r["trace"] if "reworded" in t], [{"since": "2021", "until": "2021"}])
        self.assertTrue(all(x["verdict"] != "yes" for x in r["answers"]))                              # round 2 had no unseen candidates: nothing confirmed


if __name__ == "__main__":
    unittest.main()
