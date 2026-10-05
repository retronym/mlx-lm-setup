#!/usr/bin/env python3
"""Release-note source without the network: GitHub's release list is faked; tag messages come from a real scratch git repo."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__))
import config
from store import Store
from sources import ghreleases

SHORT = {"tag_name": "v1.0.0", "name": "1.0.0", "body": "Fixes #12 and #34.", "html_url": "https://github.com/o/r/releases/tag/v1.0.0",
         "published_at": "2026-01-01T00:00:00Z", "prerelease": False, "draft": False}
LONG_BODY = "## Highlights\n" + "A highlight about the compiler. " * 40 + "See #101.\n\n## Fixes\n" + "A fix line. " * 60 + "Closes https://github.com/o/r/pull/202\n"
LONG = {**SHORT, "tag_name": "v2.0.0", "name": "Big release", "body": LONG_BODY, "html_url": "https://github.com/o/r/releases/tag/v2.0.0", "prerelease": True,
        "published_at": "2026-02-01T00:00:00Z"}
DRAFT = {**SHORT, "tag_name": "v3.0.0-draft", "draft": True}


def source(tag_messages=False):
    return config.Source(project="p", id="rel", type="github_releases", label="rel", color="#000000", priority=5, enabled=True, min_interval_hours=0,
                         max_items_per_run=None, repo="o/r", tag_messages=tag_messages)


def feed(releases):
    return mock.patch.object(ghreleases, "pages", lambda path, log=print, **kw: iter([list(releases)]))


class ReleaseTests(unittest.TestCase):
    def sync(self, st, releases, repo_dir=None, tag_messages=False):
        with feed(releases):
            ghreleases.GhReleases(source(tag_messages), repo_dir).sync(st, log=lambda *_: None)

    def rows(self, st):
        return {r[0]: r[1:] for r in st.db.execute("SELECT id, doc, title, json_extract(meta, '$.refs'), json_extract(meta, '$.state') FROM chunks")}

    def test_short_notes_are_one_header_chunk_long_notes_split_by_heading(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "t.db")
            self.sync(st, [SHORT, LONG, DRAFT])
            rows = self.rows(st)
            self.assertIn("rel:release:v1.0.0:header", rows)
            self.assertEqual([k for k in rows if k.startswith("rel:release:v1.0.0")], ["rel:release:v1.0.0:header"])      # short: just the header
            long = [k for k in rows if k.startswith("rel:release:v2.0.0")]
            self.assertTrue(any("Highlights" in k for k in long) and any("Fixes" in k for k in long), long)
            self.assertEqual(rows["rel:release:v2.0.0:header"][3], "prerelease")
            self.assertFalse([k for k in rows if "draft" in k])                                                           # drafts are not published
            self.assertEqual(json.loads(rows["rel:release:v1.0.0:header"][2]), [12, 34])                                  # issue and PR numbers it mentions
            self.assertEqual(st.get("rel", "last_release"), "2026-02-01")

    def test_refs_from_pr_urls_and_sections(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "t.db")
            self.sync(st, [LONG])
            refs = {k: json.loads(v[2]) for k, v in self.rows(st).items()}
            self.assertEqual(refs["rel:release:v2.0.0:header"], [101, 202])                                                # the header carries every reference of the whole notes
            fixes = next(v for k, v in refs.items() if "Fixes" in k)
            self.assertEqual(fixes, [202])                                                                                # a section carries its own (a /pull/202 URL counts)

    def test_edit_and_delete_are_followed(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "t.db")
            self.sync(st, [SHORT, LONG])
            n = st.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
            self.sync(st, [SHORT, LONG])                                                                                  # nothing changed: nothing touched
            before = dict(st.db.execute("SELECT id, hash FROM chunks"))
            self.sync(st, [{**SHORT, "body": "Fixes #12, #34 and now #56."}, LONG])
            after = dict(st.db.execute("SELECT id, hash FROM chunks"))
            self.assertEqual([k for k in after if after[k] != before[k]], ["rel:release:v1.0.0:header"])                  # only the edited release changed
            self.sync(st, [SHORT])                                                                                        # v2.0.0 deleted upstream
            self.assertEqual(st.db.execute("SELECT count(*) FROM chunks WHERE doc = 'release:v2.0.0'").fetchone()[0], 0)
            self.assertEqual(st.db.execute("SELECT count(*) FROM fts").fetchone()[0], st.db.execute("SELECT count(*) FROM chunks").fetchone()[0])

    def test_tag_messages_only_for_annotated_tags_without_a_release(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "r.git"
            g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                                                                                      "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "f").write_text("x"); g("add", "."); g("commit", "-qm", "c1")
            g("tag", "-a", "v1.0.0", "-m", "has a release too"); g("tag", "-a", "v0.9.0", "-m", "Early version.\n\nFixes #7 and adds the thing.")
            g("tag", "light")
            st = Store(Path(d) / "t.db")
            self.sync(st, [SHORT], repo_dir=repo / ".git", tag_messages=True)
            rows = self.rows(st)
            self.assertIn("rel:tag:v0.9.0", rows)
            self.assertNotIn("rel:tag:v1.0.0", rows)                                                                      # covered by its release
            self.assertNotIn("rel:tag:light", rows)                                                                       # lightweight: no message
            self.assertEqual(json.loads(rows["rel:tag:v0.9.0"][2]), [7])
            text = st.db.execute("SELECT text, url FROM chunks WHERE id = 'rel:tag:v0.9.0'").fetchone()
            self.assertIn("Early version.", text[0]); self.assertEqual(text[1], "https://github.com/o/r/releases/tag/v0.9.0")


if __name__ == "__main__":
    unittest.main()
