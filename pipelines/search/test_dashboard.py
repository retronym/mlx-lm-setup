#!/usr/bin/env python3
"""The dashboard's facts layer (dashboard.py): the range rules (default, cap, malformed), activity counts by the date each thing happened (created for
opened, updated for merged and closed, a comment's own date), the series buckets, hot threads whose head predates the range, releases, and the open PRs
with their stored GitHub state and the rule-based readiness (and without state: age and activity only)."""
import datetime as dt, os, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
import dashboard, dashjudge
from unittest import mock
from store import Store, Chunk

NOW = time.mktime(dt.datetime(2026, 10, 6, 12).timetuple())


def pr(n, state="open", created="2026-09-20T00:00:00Z", updated="2026-10-01T00:00:00Z", author="a", pr_state=None, title=None, chunk=0):
    meta = {"kind": "pr", "state": state, "number": n, "created": created, "updated": updated, "author": author, "labels": ["x"], "closes": []}
    if pr_state:
        meta["pr_state"] = pr_state
    cid = f"prs:issue:{n}" + (f"~{chunk}" if chunk else "")
    return Chunk(cid, f"issue:{n}", f"o/r#{n} {title or 'PR ' + str(n)}", "body", f"https://github.com/o/r/pull/{n}", meta)


def issue(n, state="open", created="2026-09-20T00:00:00Z", updated="2026-10-01T00:00:00Z"):
    return Chunk(f"prs:issue:{n}", f"issue:{n}", f"o/r#{n} issue {n}", "b", f"https://github.com/o/r/issues/{n}", {"kind": "issue", "state": state, "number": n, "created": created, "updated": updated, "author": "i"})


def comment(n, cid, created, kind="comment"):
    return Chunk(f"prs:{kind}:{cid}", f"issue:{n}", f"o/r#{n} t  ({kind} by c)", "text", "u", {"kind": kind, "number": n, "created": created, "updated": created, "author": "c"})


def st(**kw):
    base = {"draft": False, "mergeable": "MERGEABLE", "ci": "pass", "decision": None, "requested": [], "reviews": [], "base": "2.13.x", "milestone": None, "add": 1, "del": 1, "files": 1, "fetched": "2026-10-06T00:00:00Z"}
    return {**base, **kw}


class RangeTests(unittest.TestCase):
    def test_default_is_thirty_days_ending_tomorrow(self):
        s, u = dashboard.check_range(today=dt.date(2026, 10, 6))
        self.assertEqual((s, u), ("2026-09-07", "2026-10-07"))

    def test_cap_and_malformed(self):
        self.assertEqual(dashboard.check_range("2025-10-06", "2026-10-06"), ("2025-10-06", "2026-10-06"))      # 365 days
        for a, b in (("2024-01-01", None), ("2025-01-01", "2026-10-06"), ("2026-10-06", "2026-10-06"), ("2026-10", None), (None, "yesterday")):
            with self.assertRaises(ValueError, msg=(a, b)):
                dashboard.check_range(a, b, today=dt.date(2026, 10, 6))


class ReadinessTests(unittest.TestCase):
    def label(self, **kw):
        s = kw.pop("state", None)
        return dashboard.readiness({"updated": kw.pop("updated", "2026-10-01T00:00:00Z"), "comments": kw.pop("comments", 0), "state": s}, NOW)[0]

    def test_rules(self):
        self.assertEqual(self.label(state=st(draft=True)), "draft")
        self.assertEqual(self.label(state=st(mergeable="CONFLICTING")), "needs author")
        self.assertEqual(self.label(state=st(ci="fail")), "needs author")
        self.assertEqual(self.label(state=st(decision="CHANGES_REQUESTED")), "needs author")
        self.assertEqual(self.label(state=st(decision="APPROVED")), "ready to merge")
        self.assertEqual(self.label(state=st(decision="APPROVED", ci="pending")), "in review")             # approved but CI not green: not ready
        self.assertEqual(self.label(state=st(decision="APPROVED", mergeable="UNKNOWN")), "in review")
        self.assertEqual(self.label(state=st()), "needs review")
        self.assertEqual(self.label(state=st(), comments=3), "in review")
        self.assertEqual(self.label(state=st(reviews=[{"who": "r", "state": "COMMENTED"}])), "in review")

    def test_old_prs_are_stale_unless_ready(self):
        old = "2025-01-01T00:00:00Z"
        self.assertEqual(self.label(state=st(draft=True), updated=old), "stale")
        self.assertEqual(self.label(state=st(ci="fail"), updated=old), "stale")
        self.assertEqual(self.label(state=st(decision="APPROVED"), updated=old), "ready to merge")          # an approved green PR is a merge, however old
        self.assertEqual(self.label(updated=old), "stale")

    def test_without_stored_state_age_and_activity_decide(self):
        self.assertEqual(self.label(), "needs review")
        self.assertEqual(self.label(comments=2), "in review")


class FactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "i.db"
        s = Store(self.db)
        s.apply("prs", [
            pr(1, "merged", created="2026-09-20T00:00:00Z", updated="2026-09-25T00:00:00Z"),                # opened and merged in range
            pr(2, "merged", created="2026-01-01T00:00:00Z", updated="2026-09-26T00:00:00Z"),                # opened long ago, merged in range
            pr(3, "closed", created="2026-09-21T00:00:00Z", updated="2026-09-22T00:00:00Z"),
            pr(4, "merged", created="2025-01-01T00:00:00Z", updated="2025-02-01T00:00:00Z"),                # outside
            pr(10, pr_state=st(decision="APPROVED"), updated="2026-09-30T00:00:00Z"),                       # open: ready
            pr(11, pr_state=st(ci="fail"), updated="2025-01-01T00:00:00Z", created="2024-01-01T00:00:00Z"),  # open: stale
            pr(12, title="big", updated="2026-10-02T00:00:00Z"), pr(12, title="big", chunk=1),             # open, no state, two chunks: one PR
            issue(20, "open", created="2026-09-28T00:00:00Z"), issue(21, "closed", created="2026-01-01T00:00:00Z", updated="2026-09-29T00:00:00Z"),
            issue(22, "open", created="2025-01-01T00:00:00Z", updated="2025-01-02T00:00:00Z"),
            comment(22, "c1", "2026-09-27T00:00:00Z"), comment(22, "c2", "2026-09-27T01:00:00Z", "review"), comment(22, "c3", "2025-01-01T00:00:00Z"),
            comment(12, "c4", "2026-09-28T00:00:00Z"), comment(12, "c5", "2026-08-01T00:00:00Z"),
            Chunk("commits:commit:aaa", "commit:aaa", "o/r commit aaa msg", "m", "u", {"kind": "commit", "sha": "aaa", "created": "2026-09-23T00:00:00Z", "updated": "2026-09-23T00:00:00Z", "author_name": "N"}),
            Chunk("commits:commit:aaa~1", "commit:aaa", "o/r commit aaa msg", "m2", "u", {"kind": "commit", "sha": "aaa", "created": "2026-09-23T00:00:00Z", "updated": "2026-09-23T00:00:00Z", "author_name": "N"}),
            Chunk("releases:release:v1:header", "release:v1", "o/r v1", "notes", "https://github.com/o/r/releases/tag/v1", {"kind": "release", "tag": "v1", "published": "2026-09-24", "created": "2026-09-24T00:00:00Z"}),
            Chunk("releases:release:v0:header", "release:v0", "o/r v0", "notes", "u", {"kind": "release", "tag": "v0", "published": "2025-01-01"}),
        ])
        s.commit(); s.db.close()

    def facts(self, since="2026-09-07", until="2026-10-07", weekly=False):
        return dashboard.project_facts(self.db, since, until, weekly, NOW)

    def test_totals_by_the_date_each_thing_happened(self):
        t = self.facts()["totals"]
        self.assertEqual((t["prs_opened"], t["prs_merged"], t["prs_closed"]), (4, 2, 1))                    # opened: PRs 1, 3, 10, 12 (2 and 11 are older); merged: 1 and 2
        self.assertEqual((t["issues_opened"], t["issues_closed"]), (1, 1))
        self.assertEqual((t["comments"], t["commits"]), (3, 1))                                              # c1, c2 (a review), c4; the commit's second chunk is not another commit

    def test_series_buckets_daily_and_weekly(self):
        f = self.facts()
        self.assertEqual(f["series"]["2026-09-25"], {"prs_merged": 1})
        w = self.facts(weekly=True)["series"]
        self.assertEqual(w["2026-09-21"]["prs_merged"], 2)                                                  # Monday of the week of the 25th and 26th

    def test_open_prs_are_all_of_them_with_state_readiness_and_comment_counts(self):
        o = {p["number"]: p for p in self.facts()["open_prs"]}
        self.assertEqual(sorted(o), [10, 11, 12])                                                           # one entry for a PR of two chunks
        self.assertEqual((o[10]["readiness"], o[11]["readiness"], o[12]["readiness"]), ("ready to merge", "stale", "in review"))
        self.assertEqual(o[12]["comments"], 2)
        self.assertEqual((o[10]["touched"], o[11]["touched"], o[12]["touched"]), (True, False, True))
        self.assertEqual((o[10]["repo"], o[10]["title"], o[10]["labels"]), ("o/r", "PR 10", ["x"]))

    def test_hot_thread_whose_head_is_older_than_the_range(self):
        h = self.facts()["hot_threads"]
        self.assertEqual([(x["number"], x["comments"], x["kind"]) for x in h], [("22", 2, "issue"), ("12", 1, "pr")])

    def test_releases_and_new_issues(self):
        f = self.facts()
        self.assertEqual([(r["tag"], r["date"]) for r in f["releases"]], [("v1", "2026-09-24")])
        self.assertEqual([n["number"] for n in f["new_issues"]], ["20"])


class JudgeTests(unittest.TestCase):
    """dashjudge: the text the model reads, labels only above the confidence floor, results cached by that text (a changed PR is judged again)."""
    def setUp(self):
        import dashjudge
        self.dj = dashjudge
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "p.db"
        s = Store(self.db)
        s.apply("prs", [pr(1, pr_state=st(add=9, files=2, base="2.12.x")), issue(7)])
        s.commit(); s.db.close()
        root = Path(self.tmp.name)
        class Cfg:
            def project_db(_, pid): return self.db
            def data_path(_, *p): return root.joinpath(*p)
        self.cfg, self.calls = Cfg(), []
        def decide_many(state, questions, model=None):
            self.calls.append((state, [q["ins"] for q in questions]))
            return [{"answer": next(iter(q["crit"])), "top_probability": 0.9 if i == 0 else 0.4, "probabilities": {k: 1 / len(q["crit"]) for k in q["crit"]}} for i, q in enumerate(questions)]
        p = mock.patch.object(dashjudge.gw, "decide_many", decide_many); p.start(); self.addCleanup(p.stop)

    def test_view_has_title_labels_size_and_base(self):
        v = self.dj.view("pr", "Fix it", "the body", {"labels": ["performance"], "pr_state": st(add=9, files=2, base="2.12.x")})
        self.assertIn("Title: Fix it", v); self.assertIn("Labels: performance", v); self.assertIn("Base branch: 2.12.x", v); self.assertIn("Description: the body", v)

    def test_label_only_when_confident_and_cached_by_text(self):
        refs = [{"project": "p", "repo": "o/r", "number": 1}, {"project": "p", "repo": "o/r", "number": 7}]
        r = self.dj.judge(self.cfg, refs)
        self.assertEqual((r["o/r#1"]["type"], r["o/r#7"]["type"]), ("pr", "issue"))
        self.assertEqual((r["o/r#1"]["kind"]["label"], r["o/r#1"]["risk"]["label"], r["o/r#1"]["risk"]["guess"]), ("bugfix", None, "api"))     # 0.4 is below the floor: a guess, not a label
        self.assertEqual(sorted(k for k in r["o/r#7"] if k not in ("type", "cached")), ["kind", "triage"])
        self.assertEqual(len(self.calls), 2); self.assertFalse(r["o/r#1"]["cached"])
        self.calls.clear()
        r = self.dj.judge(self.cfg, refs)
        self.assertEqual(self.calls, []); self.assertTrue(r["o/r#1"]["cached"] and r["o/r#7"]["cached"])

    def test_unknown_items_are_left_out_and_the_budget_stops_work(self):
        r = self.dj.judge(self.cfg, [{"project": "p", "repo": "o/r", "number": 99}, {"project": "p", "repo": "x/y", "number": 1}])
        self.assertEqual(r, {})                                                                           # no such number, and the same number in another repo
        r = self.dj.judge(self.cfg, [{"project": "p", "repo": "o/r", "number": 1}], budget_s=-1)
        self.assertEqual(r, {})


if __name__ == "__main__":
    unittest.main()
