#!/usr/bin/env python3
"""GitHub adapter without the network: `gh api` is faked as a filterable, ascending-by-updated list. Checks what `include` selects and how
items are routed between a repo's sources, noise skipping, merged/closed state, the forward cursor, the newest-first backfill under a
per-run cap, widening the horizon, upgrading a legacy cursor, and the re-anchoring that keeps paging under GitHub's 10,000-item cap."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__))
import config
from store import Store
from sources import ghissues

NOW = "2026-03-01T00:00:00Z"
USER = {"login": "someone", "type": "User"}


def issue(n, when, state="open", pr=False, merged=False, **kw):
    d = {"number": n, "title": f"title {n}", "body": f"body {n}", "state": state, "labels": [], "updated_at": when,
         "html_url": f"https://github.com/o/r/{'pull' if pr else 'issues'}/{n}"}
    if pr:
        d["pull_request"] = {"merged_at": when if merged else None}
    return {**d, **kw}


def comment(cid, n, when, pr=False, body="a comment", user=USER):
    return {"id": cid, "issue_url": f"https://api.github.com/repos/o/r/issues/{n}", "body": body, "user": user, "updated_at": when,
            "html_url": f"https://github.com/o/r/{'pull' if pr else 'issues'}/{n}#issuecomment-{cid}"}


def review(rid, n, when):
    return {"id": rid, "pull_request_url": f"https://api.github.com/repos/o/r/pulls/{n}", "html_url": f"https://github.com/o/r/pull/{n}#r{rid}", "body": "nit",
            "path": "a.scala", "diff_hunk": "@@ -1 +1 @@\n+x", "user": USER, "updated_at": when}


class FakeGitHub:
    def __init__(self, issues=(), comments=(), reviews=(), page_size=100):
        self.data = {"issues": list(issues), "issues/comments": list(comments), "pulls/comments": list(reviews)}
        self.page_size, self.calls = page_size, []

    def pages(self, path, log=print, **params):
        key = path.split("repos/o/r/")[1]
        since = params.get("since", "")
        self.calls.append((key, since))
        items = sorted((i for i in self.data[key] if i["updated_at"] >= since), key=lambda i: i["updated_at"])
        for k in range(0, len(items), self.page_size):
            yield items[k:k + self.page_size]


def src(sid, include, since="2026-01-01T00:00:00Z", cap=None):
    return config.Source(project="p", id=sid, type="github", label=sid, color="#000000", priority=5, enabled=True, min_interval_hours=0, max_items_per_run=cap,
                         repo="o/r", include=tuple(include), since=since)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "t.db")
        p = mock.patch("sources.ghissues.time.time", return_value=ghissues._ts(NOW))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_sync(self, gh, members, **kw):
        with mock.patch.object(ghissues, "pages", gh.pages), mock.patch.object(ghissues, "PAGE_DELAY", 0):
            ghissues.GhRepo(members).sync(self.st, log=lambda *_: None, **kw)

    def ids(self):
        return {r[0]: r[1:] for r in self.st.db.execute("SELECT id, json_extract(meta, '$.state'), source FROM chunks")}


class IncludeAndRouting(Case):
    GH = FakeGitHub([issue(1, "2026-02-01T00:00:00Z"), issue(2, "2026-02-02T00:00:00Z", "closed", pr=True, merged=True), issue(3, "2026-02-03T00:00:00Z", pr=True)],
                    [comment(10, 1, "2026-02-04T00:00:00Z"), comment(11, 2, "2026-02-05T00:00:00Z", pr=True),
                     comment(12, 2, "2026-02-05T01:00:00Z", pr=True, user={"login": "ci", "type": "Bot"}), comment(13, 2, "2026-02-05T02:00:00Z", pr=True, body="/rebuild")],
                    [review(20, 2, "2026-02-06T00:00:00Z")])

    def test_issues_only(self):
        self.run_sync(self.GH, [src("s", ["issues"])])
        self.assertEqual(set(self.ids()), {"s:issue:1"})

    def test_prs_with_comments_and_reviews(self):
        self.run_sync(self.GH, [src("s", ["prs", "comments", "reviews"])])
        ids = self.ids()
        self.assertEqual(set(ids), {"s:issue:2", "s:issue:3", "s:comment:11", "s:review:20"})        # the issue's comment, the bot's and "/rebuild" are out
        self.assertEqual((ids["s:issue:2"][0], ids["s:issue:3"][0]), ("merged", "open"))             # closed + merged_at = merged

    def test_two_sources_share_one_pass_and_split_the_items(self):
        gh = FakeGitHub(self.GH.data["issues"], self.GH.data["issues/comments"], self.GH.data["pulls/comments"])
        self.run_sync(gh, [src("issues", ["issues", "comments"]), src("prs", ["prs", "comments", "reviews"])])
        ids = self.ids()
        self.assertEqual({k: v[1] for k, v in ids.items()}, {"issues:issue:1": "issues", "issues:comment:10": "issues", "prs:issue:2": "prs", "prs:issue:3": "prs",
                                                              "prs:comment:11": "prs", "prs:review:20": "prs"})
        self.assertEqual(sum(1 for k, _ in gh.calls if k == "issues/comments"), sum(1 for k, _ in gh.calls if k == "issues"))   # once per window, not once per source


class Cursors(Case):
    def all_issues(self):
        # one item per 100 days over three years
        return [issue(n, ghissues._iso(ghissues._ts("2023-03-01T00:00:00Z") + n * 100 * 86400)) for n in range(1, 11)]

    def test_first_run_backfills_newest_first_under_the_cap_and_resumes(self):
        gh = FakeGitHub(self.all_issues())
        m = [src("s", ["issues"], since="2023-01-01T00:00:00Z", cap=3)]
        self.run_sync(gh, m)
        first = {int(k.rsplit(":", 1)[1]) for k in self.ids()}
        self.assertTrue(first and max(first) == 10 and min(first) > 1, first)                          # the newest items came first, the oldest are still to do
        self.assertNotEqual(self.st.get("gh:o/r", "bf_done_issues"), "1")
        back1 = self.st.get("gh:o/r", "back_issues")
        for _ in range(10):                                                                            # cap reached each run: it takes several runs
            if self.st.get("gh:o/r", "bf_done_issues") == "1":
                break
            self.run_sync(gh, m)
        self.assertEqual(self.st.get("gh:o/r", "bf_done_issues"), "1")
        self.assertEqual(len(self.ids()), 10)
        self.assertLess(self.st.get("gh:o/r", "back_issues"), back1)                                   # the frontier moved back in time
        self.assertEqual(self.st.get("gh:o/r", "top_issues"), NOW)

    def test_new_items_arrive_through_the_forward_walk(self):
        gh = FakeGitHub([issue(1, "2026-02-01T00:00:00Z")])
        m = [src("s", ["issues"])]
        self.run_sync(gh, m)
        self.assertEqual(self.st.get("gh:o/r", "bf_done_issues"), "1")
        gh.data["issues"] += [issue(2, "2026-04-01T00:00:00Z"), {**issue(1, "2026-04-02T00:00:00Z"), "title": "edited title", "state": "closed"}]
        self.run_sync(gh, m)
        ids = self.ids()
        self.assertEqual(set(ids), {"s:issue:1", "s:issue:2"})
        self.assertEqual(ids["s:issue:1"][0], "closed")                                                # an edit and a state change are picked up
        self.assertEqual(self.st.get("gh:o/r", "fwd_issues"), "2026-04-02T00:00:00Z")
        calls = [since for k, since in gh.calls if k == "issues"]
        self.assertEqual(calls[-1], NOW)                                                               # the forward walk started at the cursor the first run saved: `now`

    def test_widening_the_horizon_reopens_the_backfill(self):
        gh = FakeGitHub([issue(1, "2025-06-01T00:00:00Z"), issue(2, "2026-02-01T00:00:00Z")])
        self.run_sync(gh, [src("s", ["issues"], since="2026-01-01T00:00:00Z")])
        self.assertEqual(set(self.ids()), {"s:issue:2"})
        self.assertEqual(self.st.get("gh:o/r", "bf_done_issues"), "1")
        self.run_sync(gh, [src("s", ["issues"], since="2025-01-01T00:00:00Z")])                       # the config now reaches back a year further
        self.assertEqual(set(self.ids()), {"s:issue:1", "s:issue:2"})

    def test_a_legacy_single_cursor_is_upgraded_without_a_re_walk(self):
        gh = FakeGitHub([issue(1, "2026-02-01T00:00:00Z"), issue(2, "2026-02-20T00:00:00Z")])
        self.st.put("s", "since_issues", "2026-02-10T00:00:00Z"); self.st.commit()
        self.run_sync(gh, [src("s", ["issues"])])
        self.assertEqual((self.st.get("gh:o/r", "bf_done_issues"), self.st.get("gh:o/r", "fwd_issues")), ("1", "2026-02-20T00:00:00Z"))
        self.assertEqual([since for k, since in gh.calls if k == "issues"], ["2026-02-10T00:00:00Z"])  # one forward walk from the old cursor, no backfill windows
        self.assertEqual(set(self.ids()), {"s:issue:2"})                                               # (item 1 predates the old cursor: it was already ingested then)

    def test_explicit_since_re_walks_forward(self):
        gh = FakeGitHub([issue(1, "2026-02-01T00:00:00Z")])
        m = [src("s", ["issues"])]
        self.run_sync(gh, m)
        self.st.db.execute("DELETE FROM chunks"); self.st.db.execute("DELETE FROM fts"); self.st.commit()
        self.run_sync(gh, m)
        self.assertEqual(self.ids(), {})                                                               # nothing newer than the cursor: nothing re-fetched
        self.run_sync(gh, m, since="2026-01-01T00:00:00Z")
        self.assertEqual(set(self.ids()), {"s:issue:1"})                                               # a repair re-walk from an explicit date


class Paging(unittest.TestCase):
    def test_paging_reanchors_before_the_page_cap(self):
        calls = []
        def fake_gh(args):
            q = dict(kv.split("=") for kv in args[0].split("?", 1)[1].split("&"))
            calls.append((int(q["page"]), q["since"]))
            n = len(calls)                                                 # timestamps keep increasing across calls, like an updated-ascending list
            items = [{"updated_at": f"2020-01-01T{n:02d}:00:{i % 60:02d}Z"} for i in range(100 if n < 8 else 3)]
            return mock.Mock(returncode=0, stdout=json.dumps(items), stderr="")
        with mock.patch.object(ghissues, "_gh", fake_gh), mock.patch.object(ghissues, "PAGE_LIMIT", 3), mock.patch.object(ghissues, "PAGE_DELAY", 0), \
                mock.patch.object(ghissues, "wait_for_quota"):
            n = sum(len(p) for p in ghissues.pages("repos/o/r/issues", log=lambda *_: None, since="2000-01-01T00:00:00Z"))
        self.assertEqual(n, 7 * 100 + 3)
        self.assertEqual([c[0] for c in calls], [1, 2, 3, 1, 2, 3, 1, 2])                           # pages restart at 1 instead of passing the cap
        self.assertTrue(all(calls[i][1] != calls[i - 1][1] for i in (3, 6)))                        # ... re-anchored at the last item's updated_at


if __name__ == "__main__":
    unittest.main()
