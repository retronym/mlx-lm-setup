#!/usr/bin/env python3
"""References and the links database: the parser (bare, qualified, URLs, legacy ids, shas, closing keywords, bytecode and code-block noise), the node and
edge extraction over fixture chunks of every source type (closes / mentions / shipped_in / touches / defines, dangling targets, aliases, bare-ref
fallbacks, conf), skip-when-unchanged, and the refresh plan."""
import json, os, sqlite3, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
import config, links, refresh
from refs import make_parser, code_comments
from store import Store, Chunk
from test_config import write, proj, uni, GH, GIT

parse = make_parser({"SI": "scala/bug"})


def keys(text):
    return [(r.kind, r.repo, r.key, r.via, r.closing) for r in parse(text)]


class ParserTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(keys("see #12, (#13) and scala/bug#14 and sbt/zinc#15"),
                         [("issue", None, "12", "bare", False), ("issue", None, "13", "bare", False), ("issue", "scala/bug", "14", "qual", False), ("issue", "sbt/zinc", "15", "qual", False)])
        self.assertEqual(keys("https://github.com/o/r/pull/7 and https://github.com/o/r/issues/8"), [("issue", "o/r", "7", "url", False), ("issue", "o/r", "8", "url", False)])
        self.assertEqual(keys("test for SI-5610 and si-1"), [("issue", "scala/bug", "5610", "legacy", False)])
        sha = "a" * 40
        self.assertEqual(keys(f"reverts {sha}; see https://github.com/o/r/commit/{'b' * 12}; commit 1234abc"),
                         [("commit", None, sha, "sha", False), ("commit", "o/r", "b" * 12, "url", False), ("commit", None, "1234abc", "sha", False)])

    def test_closing_keywords(self):
        self.assertEqual([r.closing for r in parse("Fixes #1, #2 and scala/bug#3. See #4. Fix for #5. Closes: https://github.com/o/r/issues/6")], [True, True, True, False, False, True])
        self.assertTrue(parse("fixed #936")[0].closing)
        self.assertFalse(parse("it fixes the problem described in #7")[0].closing)

    def test_one_per_target_and_closing_wins(self):
        self.assertEqual(keys("see #1. Later: Fixes #1"), [("issue", None, "1", "bare", True)])

    def test_not_references(self):
        self.assertEqual(keys("invokevirtual #38; //Method apply:()V\n#29 = Utf8 foo\nputstatic #24 // Field"), [])
        self.assertEqual(keys("&#123; C#1 a/b/c.scala#12abc scala/scala#2.13.x"), [])
        self.assertEqual(keys("```\nreal #5 in a stack\n```\nbut #6 outside"), [("issue", None, "6", "bare", False)])
        self.assertEqual(keys("[#918](https://github.com/o/r/issues/918) and #1234567"), [("issue", "o/r", "918", "url", False)])
        self.assertEqual(keys("blob https://github.com/o/r/blob/" + "c" * 40 + "/x.scala"), [])
        self.assertEqual(keys("SI-38 = NameAndType SI-46:SI-47;// clone"), [])

    def test_code_comments(self):
        src = "val x = 1 // scala/bug#1\nval u = \"http://x\"\n/* see\n * #2 */\n  * #3 (chunk started inside a block)\ndef f = 2"
        c = code_comments(src)
        self.assertIn("scala/bug#1", c); self.assertIn("#2", c); self.assertIn("#3", c)
        self.assertNotIn("http", c)


def item(n, title, body, kind="issue", state="open", repo="o/r", source="issues"):
    return Chunk(f"{source}:issue:{n}", f"issue:{n}", f"{repo}#{n} {title}", body, f"https://github.com/{repo}/issues/{n}",
                 {"kind": kind, "state": state, "number": n, "created": "2024-01-01T00:00:00Z", "author": "a"})


def comment(n, cid, body):
    return Chunk(f"issues:comment:{cid}", f"issue:{n}", f"o/r#{n} t  (comment by a)", body, "https://x", {"kind": "comment", "number": n})


SHA = "1" * 40
SHA2 = "2" * 40


def commit(sha, msg, files=()):
    text = msg + (f"\n\nFiles changed: {', '.join(files)}" if files else "")
    return Chunk(f"commits:commit:{sha}", f"commit:{sha}", f"o/r commit {sha[:8]} {msg.splitlines()[0]}", text, f"https://github.com/o/r/commit/{sha}",
                 {"kind": "commit", "sha": sha, "files": len(files), "created": "2024-02-01T00:00:00Z"})


def release(tag, body, kind="release"):
    return Chunk(f"releases:{kind}:{tag}:header", f"{kind}:{tag}", f"o/r {tag}", body, "https://x", {"kind": kind, "tag": tag, "published": "2024-03-01T00:00:00Z"})


def code(path, text):
    return Chunk(f"code:{path}:main", path, f"{path}  main", text, "https://x", {})


class LinksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        srcs = [{**GH, "include": ["issues", "prs", "comments"]}, {"id": "commits", "type": "git_log", "label": "c", "repo": "o/r", "ref": "main"},
                {"id": "releases", "type": "github_releases", "label": "r", "repo": "o/r", "tag_messages": True}, {**GIT, "id": "code", "chunkers": {".scala": "scala"}}]
        write(self.root, search={"data_dir": str(self.root / "data"), "links": {"repo_aliases": {"old/name": "o/r"}, "bare_fallbacks": {"o/r": ["o/tracker"]}}},
              projects=[proj("p", *srcs), proj("q", {**GH, "repo": "o/tracker", "include": ["issues"]})], universes=[uni("u", "p", "q", default=True)])
        self.cfg = config.load(self.root)
        st = Store(self.cfg.project_db("p"))
        st.apply("issues", [item(1, "Crash", "Reported. See scala/bug#9 and https://github.com/o/r/pull/2"),
                            item(2, "Fix crash (#1)", "Fixes #1\nAlso mentions old/name#3 and #77", kind="pr", state="merged"),
                            item(3, "Other", "Fixes #1 (an issue: only a mention)"),
                            comment(1, 10, "Fixed in " + SHA[:7] + "? commit " + SHA[:8] + " and SI-5 and #2")])
        st.apply("commits", [commit(SHA, "Fix crash. Fixes #1\n\nReverts " + SHA2, ["src/A.scala", "src/B.scala", "test/other.scala"]),
                             commit(SHA2, "Earlier. Fixes #9 and old/name#2", [f"src/f{i}.scala" for i in range(0, 12)] + ["src/A.scala"]),
                             commit("3" * 40, "mentions " + "f" * 40 + " and #999999")])
        st.apply("releases", [release("v1.0", "* Fix crash (#2)\n* Other (#3) and #1"), release("v0.9", "Tag only: #3", kind="tag")])
        st.apply("code", [code("src/A.scala", "class A {\n  // workaround for scala/bug#42, and see #1\n  val s = \"#4\"\n}"), code("src/B.scala", "class B")])
        st.commit()
        st = Store(self.cfg.project_db("q"))
        st.apply("issues", [item(9, "Tracker issue", "x", repo="o/tracker")])
        st.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        links.compute(self.cfg, "u", force=True, run=None)
        self.con = sqlite3.connect(links.db_path(self.cfg, "u"))
        return self.con

    def edges(self, type=None):
        con = self.build()
        q = "SELECT src, dst, type, conf FROM edges" + (" WHERE type = ?" if type else "")
        return {(s, d, t): c for s, d, t, c in con.execute(q, (type,) if type else ())}

    def test_nodes(self):
        con = self.build()
        kinds = dict(con.execute("SELECT id, kind FROM nodes WHERE indexed = 1"))
        self.assertEqual(kinds["o/r#1"], "issue"); self.assertEqual(kinds["o/r#2"], "pr")
        self.assertEqual(kinds[f"commit:o/r@{SHA}"], "commit"); self.assertEqual(kinds["release:o/r@v1.0"], "release"); self.assertEqual(kinds["release:o/r@v0.9"], "release")
        self.assertEqual(kinds["file:o/r:src/A.scala"], "file")
        self.assertEqual(con.execute("SELECT count(*) FROM nodes WHERE id = 'o/r#1'").fetchone()[0], 1)         # comments and continuation chunks are not nodes
        dangling = {r[0] for r in con.execute("SELECT id FROM nodes WHERE indexed = 0")}
        self.assertIn("scala/bug#9", dangling); self.assertIn("o/r#77", dangling)
        self.assertNotIn("o/r#2", dangling)

    def test_closes_only_from_prs_and_commits(self):
        e = self.edges("closes")
        self.assertIn(("o/r#2", "o/r#1", "closes"), e)                                   # PR body (and its title mentions #1: closes wins)
        self.assertIn((f"commit:o/r@{SHA}", "o/r#1", "closes"), e)
        self.assertNotIn(("o/r#3", "o/r#1", "closes"), e)                                # an issue saying "fixes" is a mention
        self.assertIn(("o/r#3", "o/r#1", "mentions"), self.edges("mentions"))
        self.assertNotIn(("o/r#2", "o/r#1", "mentions"), self.edges("mentions"))         # not both
        self.assertEqual(self.edges("closes")[(f"commit:o/r@{SHA}", "o/r#1", "closes")], 0.85)

    def test_aliases_and_fallback_to_another_tracker(self):
        e = self.edges()
        self.assertIn(("o/r#2", "o/r#3", "mentions"), e)                                 # old/name#3 is o/r#3
        self.assertEqual(e[(f"commit:o/r@{SHA2}", "o/r#2", "closes")], 0.85)           # "Fixes #9 and old/name#2": a closing list
        # #9 in o/r is no item of o/r; the configured fallback tracker has it: a guess (0.5 x 0.85)
        self.assertEqual(e[(f"commit:o/r@{SHA2}", "o/tracker#9", "closes")], 0.425)
        # #77 has no home: dangling in the document's own repo, full certainty
        self.assertEqual(e[("o/r#2", "o/r#77", "mentions")], 0.7)

    def test_comment_attributed_to_thread_with_lower_conf(self):
        e = self.edges("mentions")
        self.assertEqual(e[("o/r#1", "o/r#2", "mentions")], 0.7)                         # body URL (0.7) and comment `#2` (0.5): the best evidence wins
        self.assertEqual(e[("o/r#1", f"commit:o/r@{SHA}", "mentions")], 0.5)             # `commit 11111111`; the uncued `1111111` is not a reference
        self.assertEqual(e[("o/r#1", "scala/bug#5", "mentions")], 0.5)                   # SI-5

    def test_short_sha_must_resolve_and_bare_unknown_sha_is_dropped(self):
        ids = {d for (_, d, _) in self.edges()}
        self.assertFalse(any("f" * 40 in d for d in ids))
        self.assertIn(f"commit:o/r@{SHA2}", ids)                                         # "Reverts <40 hex>" resolves to an indexed commit
        self.assertFalse(any(d.endswith("#999999") for d in ids))                        # six digits: not a reference

    def test_shipped_in_goes_from_item_to_release(self):
        e = self.edges("shipped_in")
        self.assertEqual({k for k in e}, {("o/r#2", "release:o/r@v1.0", "shipped_in"), ("o/r#3", "release:o/r@v1.0", "shipped_in"), ("o/r#1", "release:o/r@v1.0", "shipped_in"),
                                          ("o/r#3", "release:o/r@v0.9", "shipped_in")})
        self.assertEqual(e[("o/r#2", "release:o/r@v1.0", "shipped_in")], 0.9)

    def test_touches_only_indexed_files_and_conf_falls_with_size(self):
        e = self.edges("touches")
        self.assertEqual(e[(f"commit:o/r@{SHA}", "file:o/r:src/A.scala", "touches")], 1.0)
        self.assertNotIn((f"commit:o/r@{SHA}", "file:o/r:test/other.scala", "touches"), e)
        self.assertEqual(e[(f"commit:o/r@{SHA2}", "file:o/r:src/A.scala", "touches")], round(5 / 13, 2))
        self.assertEqual(self.con.execute("SELECT v FROM meta WHERE k = 'skipped_touches'").fetchone()[0].isdigit(), True)

    def test_code_comments_define_links_but_strings_do_not(self):
        e = self.edges("defines")
        self.assertEqual(e[("file:o/r:src/A.scala", "scala/bug#42", "defines")], 0.9)
        self.assertIn(("file:o/r:src/A.scala", "o/r#1", "defines"), e)
        self.assertNotIn(("file:o/r:src/A.scala", "o/r#4", "defines"), e)

    def test_skipped_when_unchanged_and_redone_on_change_or_params(self):
        self.build()
        self.assertIsNone(links.compute(self.cfg, "u"))
        st = Store(self.cfg.project_db("p")); st.apply("issues", [item(50, "New", "see #1")]); st.commit()
        self.assertIsNotNone(links.compute(self.cfg, "u"))
        self.assertIsNone(links.compute(self.cfg, "u"))
        self.cfg.search["links"]["max_refs_per_chunk"] = 5
        self.assertIsNotNone(links.compute(self.cfg, "u"))

    def test_reading_helpers(self):
        self.build()
        lines = []
        links.stats(self.cfg, "u", out=lines.append)
        self.assertTrue(any(l.strip().startswith("closes") for l in lines))
        out = []
        links.of(self.cfg, "u", "o/r#1", out=out.append)
        self.assertTrue(any("<-closes- o/r#2" in l for l in out), out)
        out = []
        links.sample(self.cfg, "u", "closes", 5, out=out.append, seed=1)
        self.assertTrue(out and all("closes" in l and "..." in l for l in out))

    def test_refresh_plans_the_phase(self):
        args = {"only": set(), "skip": set()}
        steps = dict((ph, on) for ph, on, _ in refresh.plan(self.cfg, "u", [], args, {}, 0))
        self.assertTrue(steps["links"])
        self.cfg.search["links"]["enabled"] = False
        self.assertFalse(dict((ph, on) for ph, on, _ in refresh.plan(self.cfg, "u", [], args, {}, 0))["links"])
        self.assertTrue(dict((ph, on) for ph, on, _ in refresh.plan(self.cfg, "u", [], {"only": {"links"}, "skip": set()}, {}, 0))["links"])


if __name__ == "__main__":
    unittest.main()
