#!/usr/bin/env python3
"""GitHub's own knowledge of PRs (sources/ghlinks.py) and what links.py makes of it: closing references and merge commits fetched in batches into the PR
chunk's metadata (a fake `gh api graphql`), marker-driven work (only PRs without one, newest first, capped), a missing PR counted as done, re-fetch after
the sync rewrites a PR, rate-limit and error handling; then, in the links database, `closes` / `merged_as` edges and `shipped_in` from the first tag that
contains a commit (a real git repository)."""
import json, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__))
import config, links, repos
from sources import ghlinks
from store import Store, Chunk
from test_config import write, proj, uni, GH
from test_links import item, release, commit


def graphql_for(data, calls):
    """A fake `gh api graphql`: `data` maps PR number -> (closes, merge sha); numbers not in it are NOT_FOUND."""
    def run(query):
        import re
        nums = [int(n) for n in re.findall(r"p(\d+): pullRequest", query)]
        calls.append(nums)
        repo = {f"p{n}": ({"mergeCommit": {"oid": data[n][1]} if data[n][1] else None,
                           "closingIssuesReferences": {"nodes": [{"number": int(c.split("#")[1]), "repository": {"nameWithOwner": c.split("#")[0]}} for c in data[n][0]]}} if n in data else None) for n in nums}
        errors = [{"type": "NOT_FOUND", "path": ["repository", f"p{n}"]} for n in nums if n not in data]
        body = {"data": {"repository": repo, "rateLimit": {"remaining": 4000, "resetAt": "2030-01-01T00:00:00Z"}}, **({"errors": errors} if errors else {})}
        return subprocess.CompletedProcess([], 1 if errors else 0, json.dumps(body), "")
    return run


class EnrichTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(ghlinks.time, "sleep"); p.start(); self.addCleanup(p.stop)
        chunks = []
        for n in range(1, 8):
            c = item(n, f"PR {n}", "body", kind="pr", state="merged")
            c.meta["updated"] = f"2025-01-0{n}T00:00:00Z"
            chunks.append(c)
        chunks.append(item(50, "an issue", "body"))
        chunks.append(Chunk("issues:issue:3~1", "issue:3", "o/r#3 PR 3", "more", "u", {"kind": "pr", "number": 3}))
        self.st.apply("issues", chunks)
        self.st.commit()
        self.calls = []

    def meta(self, n):
        return json.loads(self.st.db.execute("SELECT meta FROM chunks WHERE id = ?", (f"issues:issue:{n}",)).fetchone()[0])

    def run_enrich(self, data, **kw):
        with mock.patch.object(ghlinks, "_graphql", graphql_for(data, self.calls)):
            return ghlinks.enrich(self.st, "issues", "o/r", log=lambda *_: None, **kw)

    def test_closing_references_and_merge_commit_land_in_the_pr_meta(self):
        n = self.run_enrich({1: (["o/r#40", "o/tracker#41"], "a" * 40), 2: ([], None)}, batch=50)
        self.assertEqual(n, 7)
        m = self.meta(1)
        self.assertEqual((m["closes"], m["merge_sha"]), (["o/r#40", "o/tracker#41"], "a" * 40))
        self.assertEqual((self.meta(2)["closes"], self.meta(2)["merge_sha"]), ([], None))
        self.assertEqual(self.meta(5)["closes"], [])                              # a PR GitHub no longer has: done, not retried forever
        self.assertIn("gh_links", self.meta(5))
        self.assertNotIn("gh_links", self.meta(50))                               # issues are not PRs
        self.assertEqual(self.calls, [[7, 6, 5, 4, 3, 2, 1]])                      # newest first, continuation chunks not asked for

    def test_marker_selects_work_cap_and_batches(self):
        self.run_enrich({}, max_prs=3, batch=2)
        self.assertEqual(self.calls, [[7, 6], [5]])
        self.calls.clear()
        self.run_enrich({}, max_prs=100, batch=100)
        self.assertEqual(self.calls, [[4, 3, 2, 1]])                              # only those without a marker
        self.calls.clear()
        self.assertEqual(self.run_enrich({}), 0)
        self.assertEqual(self.calls, [])

    def test_hash_is_untouched_so_nothing_is_re_embedded(self):
        before = dict(self.st.db.execute("SELECT id, hash FROM chunks"))
        self.run_enrich({})
        self.assertEqual(dict(self.st.db.execute("SELECT id, hash FROM chunks")), before)

    def test_a_rewritten_pr_is_fetched_again(self):
        self.run_enrich({1: (["o/r#40"], None)})
        c = item(1, "PR 1", "body", kind="pr", state="closed"); c.meta["updated"] = "2025-02-01T00:00:00Z"
        self.st.apply("issues", [c]); self.st.commit()                            # the sync wrote fresh metadata: no marker
        self.assertNotIn("gh_links", self.meta(1))
        self.calls.clear()
        self.run_enrich({1: (["o/r#40", "o/r#41"], None)})
        self.assertEqual(self.calls, [[1]])
        self.assertEqual(self.meta(1)["closes"], ["o/r#40", "o/r#41"])

    def test_errors_other_than_not_found_are_raised(self):
        bad = lambda q: subprocess.CompletedProcess([], 1, json.dumps({"data": None, "errors": [{"type": "FORBIDDEN"}]}), "boom")
        with mock.patch.object(ghlinks, "_graphql", bad), self.assertRaises(RuntimeError):
            ghlinks.enrich(self.st, "issues", "o/r", log=lambda *_: None)

    def test_rate_limit_is_waited_out_then_retried(self):
        answers = [subprocess.CompletedProcess([], 1, "", "API rate limit exceeded")]
        ok = graphql_for({}, self.calls)
        def run(q):
            return answers.pop(0) if answers else ok(q)
        with mock.patch.object(ghlinks, "_graphql", run):
            self.assertEqual(ghlinks.enrich(self.st, "issues", "o/r", log=lambda *_: None), 7)


def git(repo, *a, env=None):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env={**os.environ, "GIT_AUTHOR_DATE": "2024-01-01T00:00:00", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00", **(env or {})}).stdout.strip()


class LinksFromGitHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        srcs = [{**GH, "include": ["issues", "prs"]}, {"id": "commits", "type": "git_log", "label": "c", "repo": "o/r", "ref": "main"},
                {"id": "releases", "type": "github_releases", "label": "r", "repo": "o/r", "tag_messages": True}]
        write(self.root, search={"data_dir": str(self.root / "data")}, projects=[proj("p", *srcs)], universes=[uni("u", "p", default=True)])
        self.cfg = config.load(self.root)
        clone = repos.path_for(self.cfg, "o/r")                                    # a real repository: c1 <- v0.9, c2 <- c3 <- v1.0, c4 unreleased
        clone.mkdir(parents=True)
        git(clone, "init", "-q", "-b", "main")
        git(clone, "config", "user.email", "t@t"); git(clone, "config", "user.name", "t")
        self.sha = {}
        for name, tag, day in (("c1", "v0.9", "2024-01-01"), ("c2", None, "2024-02-01"), ("c3", "v1.0", "2024-03-01"), ("c4", None, "2024-04-01")):
            git(clone, "commit", "-q", "--allow-empty", "-m", name, env={"GIT_AUTHOR_DATE": day + "T00:00:00", "GIT_COMMITTER_DATE": day + "T00:00:00"})
            self.sha[name] = git(clone, "rev-parse", "HEAD")
            if tag:
                git(clone, "tag", tag, env={"GIT_COMMITTER_DATE": day + "T00:00:00"})
        st = Store(self.cfg.project_db("p"))
        pr = item(10, "Fix", "Fixes #5", kind="pr", state="merged")
        pr.meta.update(closes=["o/r#5", "old/name#6"], merge_sha=self.sha["c2"], gh_links="2025-01-01T00:00:00Z")
        pr4 = item(11, "Later", "x", kind="pr", state="merged"); pr4.meta.update(closes=[], merge_sha=self.sha["c4"], gh_links="x")
        st.apply("issues", [item(5, "Bug", "x"), pr, pr4])
        st.apply("commits", [commit(self.sha["c1"], "c1"), commit(self.sha["c3"], "c3"), commit(self.sha["c4"], "c4")])
        st.apply("releases", [release("v1.0", "notes"), release("v0.9", "old", kind="tag")])
        st.commit()
        links.compute(self.cfg, "u", force=True, run=None)
        self.con = sqlite3.connect(links.db_path(self.cfg, "u"))

    def tearDown(self):
        self.tmp.cleanup()

    def edges(self, type):
        return {(s, d): (c, h) for s, d, c, h in self.con.execute("SELECT src, dst, conf, how FROM edges WHERE type = ?", (type,))}

    def test_closing_references_are_certain_and_beat_the_text(self):
        e = self.edges("closes")
        self.assertEqual(e[("o/r#10", "o/r#5")], (1.0, "github closing ref"))      # the body's "Fixes #5" says 0.85; GitHub says 1
        self.assertIn(("o/r#10", "old/name#6"), e)                                 # no alias configured in this test: kept as GitHub wrote it
        self.assertNotIn(("o/r#10", "o/r#5"), self.edges("mentions"))

    def test_merged_as_links_a_pr_to_its_commit_indexed_or_not(self):
        e = self.edges("merged_as")
        self.assertEqual(e[("o/r#10", f"commit:o/r@{self.sha['c2']}")], (1.0, "github merge commit"))     # c2 is not an indexed commit: a dangling node
        self.assertEqual(self.con.execute("SELECT indexed FROM nodes WHERE id = ?", (f"commit:o/r@{self.sha['c2']}",)).fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT indexed FROM nodes WHERE id = ?", (f"commit:o/r@{self.sha['c4']}",)).fetchone()[0], 1)

    def test_shipped_in_is_the_first_tag_that_contains_the_commit(self):
        e = self.edges("shipped_in")
        self.assertEqual(e[(f"commit:o/r@{self.sha['c1']}", "release:o/r@v0.9")][1], "first tag containing the commit")
        self.assertIn((f"commit:o/r@{self.sha['c3']}", "release:o/r@v1.0"), e)
        self.assertNotIn((f"commit:o/r@{self.sha['c3']}", "release:o/r@v0.9"), e)
        self.assertIn(("o/r#10", "release:o/r@v1.0"), e)                           # the PR, through its merge commit c2 (not an indexed commit)
        self.assertFalse(any(s == "o/r#11" or s.endswith(self.sha["c4"]) for s, _ in e))   # c4 is in no tag yet


if __name__ == "__main__":
    unittest.main()
