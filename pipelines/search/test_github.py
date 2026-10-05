#!/usr/bin/env python3
"""GitHub adapter without the network: `gh api` is faked. Checks what `include` selects, noise skipping, merged/closed state, resumable
cursors, and the re-anchoring that keeps paging under GitHub's 10,000-item `page=` cap."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__))
import config
from store import Store
from sources import ghissues

ISSUE = {"number": 1, "title": "an issue", "body": "body one", "state": "open", "html_url": "https://github.com/o/r/issues/1", "labels": [{"name": "bug"}], "updated_at": "2026-01-01T00:00:00Z"}
PR = {"number": 2, "title": "a pr", "body": "body two", "state": "closed", "html_url": "https://github.com/o/r/pull/2", "labels": [], "updated_at": "2026-01-02T00:00:00Z",
      "pull_request": {"merged_at": "2026-01-02T00:00:00Z"}}
OPEN_PR = {**PR, "number": 3, "state": "open", "html_url": "https://github.com/o/r/pull/3", "pull_request": {"merged_at": None}, "updated_at": "2026-01-03T00:00:00Z"}
USER = {"login": "someone", "type": "User"}
C_ISSUE = {"id": 10, "issue_url": "https://api.github.com/repos/o/r/issues/1", "html_url": "https://github.com/o/r/issues/1#issuecomment-10", "body": "a comment", "user": USER, "updated_at": "2026-01-04T00:00:00Z"}
C_PR = {"id": 11, "issue_url": "https://api.github.com/repos/o/r/issues/2", "html_url": "https://github.com/o/r/pull/2#issuecomment-11", "body": "pr comment", "user": USER, "updated_at": "2026-01-05T00:00:00Z"}
C_BOT = {**C_PR, "id": 12, "user": {"login": "ci", "type": "Bot"}}
C_CMD = {**C_PR, "id": 13, "body": "/rebuild"}
REVIEW = {"id": 20, "pull_request_url": "https://api.github.com/repos/o/r/pulls/2", "html_url": "https://github.com/o/r/pull/2#r20", "body": "nit", "path": "a.scala",
          "diff_hunk": "@@ -1 +1 @@\n+x", "user": USER, "updated_at": "2026-01-06T00:00:00Z"}
STREAMS = {"issues": [ISSUE, PR, OPEN_PR], "issues/comments": [C_ISSUE, C_PR, C_BOT, C_CMD], "pulls/comments": [REVIEW]}


def fake_pages(path, log=print, **params):
    yield STREAMS[path.split(f"repos/o/r/")[1]]


def source(include, **kw):
    return config.Source(project="p", id="s", type="github", label="s", color="#000000", priority=5, enabled=True, min_interval_hours=0,
                         max_items_per_run=kw.pop("max_items_per_run", None), repo="o/r", include=tuple(include), since="2000-01-01T00:00:00Z")


class GithubTests(unittest.TestCase):
    def sync(self, include, **kw):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ghissues, "pages", fake_pages):
            st = Store(Path(d) / "t.db")
            ghissues.GhIssues(source(include, **kw)).sync(st, log=lambda *_: None)
            return {r[0]: r[1:] for r in st.db.execute("SELECT id, json_extract(meta, '$.state'), json_extract(meta, '$.kind') FROM chunks")}, dict(st.db.execute("SELECT k, v FROM state"))

    def test_issues_only(self):
        ids, _ = self.sync(["issues"])
        self.assertEqual(set(ids), {"s:issue:1"})

    def test_prs_with_comments_and_reviews(self):
        ids, state = self.sync(["prs", "comments", "reviews"])
        self.assertEqual(set(ids), {"s:issue:2", "s:issue:3", "s:comment:11", "s:review:20"})        # the issue's comment, the bot's and "/rebuild" are out
        self.assertEqual((ids["s:issue:2"][0], ids["s:issue:3"][0]), ("merged", "open"))             # closed + merged_at = merged
        self.assertEqual(state["since_issues"], "2026-01-03T00:00:00Z")                              # one cursor per stream, saved after the page
        self.assertEqual((state["since_comments"], state["since_reviews"]), ("2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z"))

    def test_everything(self):
        ids, _ = self.sync(["issues", "prs", "comments", "reviews"])
        self.assertEqual(set(ids), {"s:issue:1", "s:issue:2", "s:issue:3", "s:comment:10", "s:comment:11", "s:review:20"})

    def test_no_reviews_stream_unless_asked(self):
        with mock.patch.object(ghissues, "pages", side_effect=lambda path, *a, **k: fake_pages(path)) as m, tempfile.TemporaryDirectory() as d:
            ghissues.GhIssues(source(["issues"])).sync(Store(Path(d) / "t.db"), log=lambda *_: None)
            self.assertEqual([c.args[0] for c in m.call_args_list], ["repos/o/r/issues"])             # no comments or reviews stream either

    def test_per_run_cap(self):
        with mock.patch.object(ghissues, "pages", fake_pages), tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "t.db")
            ghissues.GhIssues(source(["issues", "prs"], max_items_per_run=2)).sync(st, log=lambda *_: None)
            self.assertEqual(st.db.execute("SELECT count(*) FROM chunks").fetchone()[0], 2)

    def test_paging_reanchors_before_the_page_cap(self):
        calls = []
        def fake_gh(args):
            q = dict(kv.split("=") for kv in args[0].split("?", 1)[1].split("&"))
            calls.append((int(q["page"]), q["since"]))
            page = int(q["page"])
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
