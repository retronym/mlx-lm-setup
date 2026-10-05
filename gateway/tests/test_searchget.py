"""`get`: whole documents behind search hits, read from the index files and a managed clone."""
import json, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))
from gateway import searchget, searchinfo  # noqa: E402

SEARCH = ROOT / "pipelines" / "search"


class GetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cfgdir = root / "cfg"
        (cfgdir / "projects").mkdir(parents=True); (cfgdir / "universes").mkdir()
        (cfgdir / "search.json").write_text(json.dumps({"data_dir": str(root / "data")}))
        src = {"id": "code", "type": "git", "label": "code", "color": "#112233", "priority": 2, "repo": "o/r", "ref": "main", "paths": ["."], "chunkers": {".md": "markdown"}}
        iss = {"id": "issues", "type": "github", "label": "issues", "color": "#112233", "priority": 2, "repo": "o/r", "include": ["issues", "comments"]}
        (cfgdir / "projects" / "p.json").write_text(json.dumps({"id": "p", "title": "P", "sources": [src, iss]}))
        (cfgdir / "universes" / "u.json").write_text(json.dumps({"id": "u", "title": "U", "projects": ["p"], "default": True}))
        _, self.cfg = searchinfo.load_config(str(SEARCH), str(cfgdir))
        self.repos = searchinfo._module(str(SEARCH), "repos")
        # a real bare clone with one file, so scope=file reads it with git
        work = root / "work"; work.mkdir()
        run = lambda *a, cwd=work: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        run("init", "-q", "-b", "main"); (work / "a.md").write_text("\n".join(f"line {i}" for i in range(1, 11)) + "\n")
        run("add", "."); run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"); sha = run("rev-parse", "HEAD")
        bare = self.repos.path_for(self.cfg, "o/r"); bare.parent.mkdir(parents=True)
        run("clone", "-q", "--bare", str(work), str(bare))
        db = self.cfg.project_db("p"); db.parent.mkdir(parents=True)
        sys.path.insert(0, str(SEARCH))
        from store import Store, Chunk
        st = Store(db)
        st.apply("code", [Chunk("code:a.md:(top)#0", "a.md", "a.md", "line 1\nline 2", f"https://github.com/o/r/blob/{sha}/a.md#L1", {"line": 1}),
                          Chunk("code:a.md:More#0", "a.md", "a.md  More", "line 5\nline 6", f"https://github.com/o/r/blob/{sha}/a.md#L5", {"line": 5})], doc="a.md")
        st.apply("issues", [Chunk("issues:comment:9", "issue:7", "o/r#7 title  (comment by bob)", "second", "https://github.com/o/r/issues/7#issuecomment-9",
                                  {"kind": "comment", "number": 7, "created": "2020-01-02T00:00:00Z", "author": "bob"}),
                            Chunk("issues:issue:7", "issue:7", "o/r#7 title", "first", "https://github.com/o/r/issues/7",
                                  {"kind": "issue", "state": "open", "number": 7, "created": "2020-01-01T00:00:00Z", "author": "al"})], doc="issue:7")
        st.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, refs, **kw):
        return searchget.get(self.cfg, refs, repos=self.repos, **kw)["results"]

    def test_doc_scope_returns_a_thread_in_order_and_a_file_by_sections(self):
        r, = self.get(["p/issues:comment:9"])
        self.assertEqual((r["found"], r["kind"], r["chunks"], r["author"]), (True, "comment", 2, "bob"))
        self.assertLess(r["text"].index("first"), r["text"].index("second"))                         # the issue before its comment, by creation time
        self.assertIn("(comment by bob)", r["text"])
        f, = self.get(["p/code:a.md:More#0"])
        self.assertEqual((f["chunks"], f["text"]), (2, "line 1\nline 2\n\nline 5\nline 6"))
        c, = self.get(["p/code:a.md:More#0"], scope="chunk")
        self.assertEqual(c["text"], "line 5\nline 6")

    def test_file_scope_reads_the_clone_with_line_ranges(self):
        f, = self.get(["p/code:a.md:More#0"], scope="file", lines="3-4")
        self.assertEqual((f["text"], f["lines"], f["total_lines"]), ("line 3\nline 4", [3, 4], 11))
        f, = self.get(["p/code:a.md:More#0"], scope="file")
        self.assertEqual(f["total_chars"], len("\n".join(f"line {i}" for i in range(1, 11)) + "\n"))     # the whole file, including lines no chunk holds
        i, = self.get(["p/issues:issue:7"], scope="file")
        self.assertIn("only applies to files", i["note"])                                              # an issue falls back to its document

    def test_paging_and_errors(self):
        full, = self.get(["p/issues:issue:7"], max_chars=50000)
        self.assertFalse(full["truncated"])
        first, = self.get(["p/issues:issue:7"], max_chars=100)
        self.assertEqual((first["truncated"], first["next_offset"], first["total_chars"]), (True, 100, len(full["text"])))
        rest, = self.get(["p/issues:issue:7"], max_chars=50000, offset=first["next_offset"])
        self.assertEqual(first["text"] + rest["text"], full["text"])                                  # pages tile the document
        miss = self.get(["p/nope", "zz/anything"])
        self.assertEqual([(m["found"], "error" in m) for m in miss], [(False, True)] * 2)
        for bad in (dict(refs=[]), dict(refs=["noslash"]), dict(refs=["p/x"], scope="all"), dict(refs=["p/x"], max_chars=5), dict(refs=["p/code:a.md:More#0"], scope="file", lines="9-2")):
            with self.assertRaises(ValueError, msg=str(bad)):
                searchget.get(self.cfg, repos=self.repos, **bad)


if __name__ == "__main__":
    unittest.main()
