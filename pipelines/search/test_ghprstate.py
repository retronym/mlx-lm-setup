#!/usr/bin/env python3
"""Open-PR state for the dashboard (sources/ghprstate.py): fetched in batches into the head chunk's metadata for open PRs only, re-fetched when older than the
max age (stalest first), a vanished PR left without state, the GraphQL node mapped to the stored shape (CI rollup, requested users and teams, latest reviews)."""
import json, os, re, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__))
from sources import ghprstate, ghlinks
from store import Store, Chunk
from test_links import item


def node(**kw):
    n = {"isDraft": False, "mergeable": "MERGEABLE", "baseRefName": "2.13.x", "additions": 10, "deletions": 2, "changedFiles": 3, "reviewDecision": None, "milestone": {"title": "2.13.19"},
         "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": "retronym"}}, {"requestedReviewer": {"__typename": "Team", "slug": "stdlib-officers"}}]},
         "latestReviews": {"nodes": [{"state": "APPROVED", "author": {"login": "som-snytt"}}, {"state": "COMMENTED", "author": None}]},
         "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}}
    n.update(kw)
    return n


def fake(nodes, calls):
    def run(query):
        nums = [int(n) for n in re.findall(r"p(\d+): pullRequest", query)]
        calls.append(nums)
        repo = {f"p{n}": nodes.get(n) for n in nums}
        errors = [{"type": "NOT_FOUND"} for n in nums if n not in nodes]
        body = {"data": {"repository": repo, "rateLimit": {"remaining": 4000, "resetAt": "2030-01-01T00:00:00Z"}}, **({"errors": errors} if errors else {})}
        return subprocess.CompletedProcess([], 1 if errors else 0, json.dumps(body), "")
    return run


class PrStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.st = Store(Path(self.tmp.name) / "t.db")
        p = mock.patch.object(ghprstate.time, "sleep"); p.start(); self.addCleanup(p.stop)
        self.st.apply("issues", [item(1, "open PR", "b", kind="pr", state="open"), item(2, "open PR", "b", kind="pr", state="open"), item(3, "merged", "b", kind="pr", state="merged"),
                                 item(4, "an issue", "b", state="open"), Chunk("issues:issue:1~1", "issue:1", "t", "more", "u", {"kind": "pr", "state": "open", "number": 1})])
        self.st.commit(); self.calls = []

    def meta(self, n):
        return json.loads(self.st.db.execute("SELECT meta FROM chunks WHERE id = ?", (f"issues:issue:{n}",)).fetchone()[0])

    def run_refresh(self, nodes, **kw):
        with mock.patch.object(ghlinks, "_graphql", fake(nodes, self.calls)):
            return ghprstate.refresh(self.st, "issues", "o/r", log=lambda *_: None, **kw)

    def test_open_prs_only_and_stored_shape(self):
        self.assertEqual(self.run_refresh({1: node(isDraft=True, mergeable="CONFLICTING", reviewDecision="APPROVED"), 2: node()}), 2)
        self.assertEqual(sorted(self.calls[0]), [1, 2])                               # not the merged PR, the issue or the continuation chunk
        s = self.meta(1)["pr_state"]
        self.assertEqual((s["draft"], s["mergeable"], s["ci"], s["decision"], s["base"], s["milestone"]), (True, "CONFLICTING", "pass", "APPROVED", "2.13.x", "2.13.19"))
        self.assertEqual((s["requested"], s["reviews"], s["add"], s["del"], s["files"]), (["retronym", "stdlib-officers"], [{"who": "som-snytt", "state": "APPROVED"}], 10, 2, 3))
        self.assertNotIn("pr_state", self.meta(3)); self.assertNotIn("pr_state", self.meta(4))

    def test_ci_states(self):
        for rollup, want in (("FAILURE", "fail"), ("PENDING", "pending"), (None, "none")):
            n = node(commits={"nodes": [{"commit": {"statusCheckRollup": {"state": rollup} if rollup else None}}]})
            self.assertEqual(ghprstate.state_of(n, 0)["ci"], want)

    def test_fresh_state_is_not_refetched_but_stale_is(self):
        self.run_refresh({1: node(), 2: node()}); self.calls.clear()
        self.assertEqual(self.run_refresh({1: node(), 2: node()}), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_refresh({1: node(), 2: node()}, max_age_hours=0), 2)

    def test_a_pr_github_no_longer_has_gets_no_state(self):
        self.run_refresh({1: node()})
        self.assertIn("pr_state", self.meta(1)); self.assertNotIn("pr_state", self.meta(2))

    def test_a_rewritten_pr_is_fetched_again(self):
        self.run_refresh({1: node(), 2: node()})
        c = item(1, "open PR", "b2", kind="pr", state="open"); self.st.apply("issues", [c]); self.st.commit()
        self.assertNotIn("pr_state", self.meta(1)); self.calls.clear()
        self.run_refresh({1: node()}); self.assertEqual(self.calls, [[1]])

    def test_batches(self):
        self.run_refresh({1: node(), 2: node()}, batch=1)
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
