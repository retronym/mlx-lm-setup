#!/usr/bin/env python3
"""Commit-message source against a real scratch git repository: newest-first backfill under a cap, the forward walk for new commits, bots and merges
skipped, the paths filter, the horizon (and widening it), a rewritten branch, long messages, and the #123 references."""
import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
import config
from store import Store
from sources.gitlog import GitLog, _refs, handle_from_email

DAY = 86400
T0 = 1767225600          # 2026-01-01T00:00:00Z


class Repo:
    def __init__(self, path):
        self.path = path
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        self.n = 0

    def git(self, *a, **env):
        e = {**os.environ, "GIT_AUTHOR_NAME": "Dev One", "GIT_AUTHOR_EMAIL": "d@x", "GIT_COMMITTER_NAME": "Dev One", "GIT_COMMITTER_EMAIL": "d@x", **env}
        return subprocess.run(["git", "-C", str(self.path), *a], check=True, capture_output=True, text=True, env=e).stdout.strip()

    def commit(self, subject, body="", files=("src/a.scala",), day=None, author="Dev One", email="d@x"):
        self.n += 1
        day = self.n if day is None else day
        for f in files:
            p = self.path / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{self.n}\n")
        self.git("add", ".")
        when = f"@{T0 + day * DAY} +0000"
        self.git("commit", "-q", "-m", subject + (f"\n\n{body}" if body else ""), GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when, GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email)
        return self.git("rev-parse", "HEAD")


def source(**kw):
    d = dict(project="p", id="commits", type="git_log", label="c", color="#000000", priority=5, enabled=True, min_interval_hours=0, max_items_per_run=None,
             repo="o/r", ref="main", since="2000-01-01T00:00:00Z", merges=False, skip_authors=("scala-steward", "dependabot[bot]"))
    d.update(kw)
    return config.Source(**d)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Path(self.tmp.name) / "r")
        self.st = Store(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.tmp.cleanup)

    def sync(self, **kw):
        GitLog(source(**kw), self.repo.path).sync(self.st, log=lambda *_: None)

    def subjects(self):
        return sorted(r[0] for r in self.st.db.execute("SELECT substr(title, instr(title, ' commit ') + 17) FROM chunks"))

    def state(self, k):
        return self.st.get("commits", k)


class Backfill(Case):
    def test_newest_first_under_a_cap_then_forward_for_new_commits(self):
        for i in range(1, 11):
            self.repo.commit(f"commit number {i}")
        self.sync(max_items_per_run=4)
        self.assertEqual(self.subjects(), [f"commit number {i}" for i in (10, 7, 8, 9)])        # the four newest, not the oldest
        self.assertNotEqual(self.state("bf_done"), "1")
        self.sync(max_items_per_run=4)
        self.assertEqual(len(self.subjects()), 8)
        self.sync(max_items_per_run=4)                                                           # the last two: fewer than the cap, so the history is complete
        self.assertEqual((len(self.subjects()), self.state("bf_done")), (10, "1"))
        self.repo.commit("a brand new commit"); self.repo.commit("and another one")
        before = self.st.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        self.sync(max_items_per_run=4)                                                           # forward walk: exactly the two new ones
        self.assertEqual(self.st.db.execute("SELECT count(*) FROM chunks").fetchone()[0], before + 2)
        self.assertEqual(self.state("head"), self.repo.git("rev-parse", "HEAD"))
        self.sync(max_items_per_run=4)                                                           # nothing new: nothing touched
        self.assertEqual(self.st.db.execute("SELECT count(*) FROM chunks").fetchone()[0], before + 2)

    def test_a_chunk_has_the_message_the_files_and_the_metadata(self):
        sha = self.repo.commit("Fix the typer crash", "The reason is that x was null.\n\nFixes scala/bug#1234, see also (#77).", files=("src/a.scala", "test/b.scala"))
        self.sync()
        row = self.st.db.execute("SELECT id, doc, title, text, url, meta FROM chunks").fetchone()
        self.assertEqual(row[:2], (f"commits:commit:{sha}", f"commit:{sha}"))
        self.assertEqual(row[2], f"o/r commit {sha[:8]} Fix the typer crash")
        self.assertIn("The reason is that x was null.", row[3]); self.assertIn("Files changed: src/a.scala, test/b.scala", row[3])
        self.assertEqual(row[4], f"https://github.com/o/r/commit/{sha}")
        m = json.loads(row[5])
        self.assertEqual((m["kind"], m["author_name"], m["refs"], m["files"]), ("commit", "Dev One", [77, 1234], 2))
        self.assertIsNone(m["author"])                                                           # d@x says nothing about a GitHub handle
        self.assertTrue(m["updated"].endswith("Z")); self.assertEqual(m["created"], m["updated"])

    def test_bots_and_merges_are_skipped_unless_asked(self):
        self.repo.commit("a real change")
        self.repo.commit("Update sbt to 1.10.1", author="scala-steward")
        self.repo.git("checkout", "-q", "-b", "feature")
        self.repo.commit("work on the feature", files=("src/f.scala",))
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge pull request #5 from feature", "feature")
        self.sync()
        self.assertEqual(self.subjects(), ["a real change", "work on the feature"])              # no bot, no merge
        with tempfile.TemporaryDirectory() as d:
            st2 = Store(Path(d) / "m.db")
            GitLog(source(merges=True), self.repo.path).sync(st2, log=lambda *_: None)
            self.assertIn("Merge pull request #5 from feature", [r[0] for r in st2.db.execute("SELECT substr(title, instr(title, ' commit ') + 17) FROM chunks")])

    def test_the_github_handle_comes_from_a_noreply_email(self):
        self.repo.commit("by a github user", email="12345+octocat@users.noreply.github.com", author="The Octocat")
        self.sync()
        m = json.loads(self.st.db.execute("SELECT meta FROM chunks").fetchone()[0])
        self.assertEqual((m["author"], m["author_name"]), ("octocat", "The Octocat"))

    def test_meta_only_refreshes_existing_commits_and_ingests_nothing(self):
        for i in range(1, 5):
            self.repo.commit(f"commit {i}", email="7+dev@users.noreply.github.com")
        self.sync(max_items_per_run=2)                                                           # only the two newest are indexed
        for r in self.st.db.execute("SELECT rowid, meta FROM chunks").fetchall():                # as written before handles were captured: `author` was the git name
            m = json.loads(r[1]); m["author"] = m.pop("author_name"); m.pop("created", None)
            self.st.db.execute("UPDATE chunks SET meta = ? WHERE rowid = ?", (json.dumps(m), r[0]))
        self.st.commit()
        state = dict(self.st.db.execute("SELECT k, v FROM state"))
        GitLog(source(max_items_per_run=2), self.repo.path).sync(self.st, log=lambda *_: None, meta_only=True)
        rows = [json.loads(r[0]) for r in self.st.db.execute("SELECT meta FROM chunks")]
        self.assertEqual(len(rows), 2)                                                           # commits 1 and 2 were not indexed and are not now
        self.assertTrue(all(m["author"] == "dev" and m["author_name"] == "Dev One" and m["created"] for m in rows))
        self.assertEqual(dict(self.st.db.execute("SELECT k, v FROM state")), state)

    def test_paths_filter(self):
        self.repo.commit("touches the compiler", files=("src/compiler/a.scala",))
        self.repo.commit("touches only docs", files=("docs/readme.md",))
        self.sync(paths=("src",))
        self.assertEqual(self.subjects(), ["touches the compiler"])

    def test_horizon_and_widening_it(self):
        for d in (1, 5, 9, 12):
            self.repo.commit(f"day {d}", day=d)
        self.sync(since="2026-01-08T00:00:00Z")
        self.assertEqual(self.subjects(), ["day 12", "day 9"])
        self.assertEqual(self.state("bf_done"), "1")
        self.sync(since="2026-01-03T00:00:00Z")                                                  # the config now reaches back further: the backfill re-opens
        self.assertEqual(self.subjects(), ["day 12", "day 5", "day 9"])

    def test_a_rewritten_branch_does_not_break_and_nothing_is_stored_twice(self):
        for i in range(1, 4):
            self.repo.commit(f"commit {i}")
        self.sync()
        old = self.repo.git("rev-parse", "HEAD")
        self.repo.git("commit", "-q", "--amend", "-m", "commit 3, reworded")                    # the tip changes sha
        self.sync()
        subs = self.subjects()
        self.assertIn("commit 3, reworded", subs); self.assertEqual(subs.count("commit 1"), 1)   # re-walked: commit 1 is still one chunk
        self.assertEqual(self.state("head"), self.repo.git("rev-parse", "HEAD"))

    def test_long_messages_are_split_and_a_root_commit_ends_the_backfill(self):
        self.repo.commit("long one", "\n\n".join(f"paragraph {i} " + "word " * 40 for i in range(12)))
        GitLog(source(), self.repo.path, max_chars=400).sync(self.st, log=lambda *_: None)
        self.assertGreater(self.st.db.execute("SELECT count(*) FROM chunks").fetchone()[0], 3)
        self.assertEqual(len({r[0] for r in self.st.db.execute("SELECT doc FROM chunks")}), 1)   # all parts belong to one document
        self.assertEqual(self.state("bf_done"), "1")


class Handles(unittest.TestCase):
    def test_noreply_addresses(self):
        for email, want in (("12345+octocat@users.noreply.github.com", "octocat"), ("octocat@users.noreply.github.com", "octocat"), ("Foo-Bar@users.noreply.github.com", "Foo-Bar"),
                            ("someone@gmail.com", None), ("", None), ("a@users.noreply.github.com.evil.example", None)):
            self.assertEqual(handle_from_email(email), want, email)


class Refs(unittest.TestCase):
    def test_references(self):
        self.assertEqual(_refs("Fixes #123, see scala/bug#45 and (#77). Version 1.2.3, C#5x"), [45, 77, 123])
        self.assertEqual(_refs("no refs here, nor in foo#9"), [])


if __name__ == "__main__":
    unittest.main()
