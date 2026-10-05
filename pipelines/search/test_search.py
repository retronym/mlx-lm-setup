#!/usr/bin/env python3
"""Federated search over several project databases, with a fake embedder and reranker (run with the .venv-jev python: embed imports torch).
Checks: merging across projects, project and source filters, open-only (comments inherit their issue's state), one hit per document,
the reranker seeing a small project's best hit, and a project that has not been indexed yet."""
import json, os, sys, tempfile, unittest, zlib
from pathlib import Path
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
import config, embed, search
from store import Store, Chunk
from test_config import write, proj, uni, GIT, GH


class FakeEmbedder:
    name, dev = "fake", "cpu"

    def _v(self, text):
        v = np.zeros(64, dtype=np.float32)
        for w in text.lower().split():
            v[zlib.crc32(w.encode()) % 64] += 1
        return v / (np.linalg.norm(v) or 1)

    def docs(self, texts):
        return np.vstack([self._v(t) for t in texts])

    def query(self, q):
        return self._v(q)


class FakeReranker:
    def __init__(self):
        self.seen = []

    def scores(self, q, docs):
        self.seen = docs
        return [1.0 if "needle" in d else 0.1 for d in docs]


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        write(root, search={"data_dir": str(root / "data")},
              projects=[proj("big", {**GIT, "id": "code"}, {**GH, "id": "issues"}), proj("small", {**GIT, "id": "code"}), proj("later", {**GIT, "id": "code"})],
              universes=[uni("all", "big", "small", "later", default=True), uni("small-only", "small")])
        self.cfg = config.load(root)
        emb = FakeEmbedder()
        def put(pid, sid, chunks):
            st = Store(self.cfg.project_db(pid))
            st.apply(sid, chunks); st.commit(); embed.fill(st, emb, source=sid, log=lambda *_: None)
        put("big", "code", [Chunk(f"code:{i}", f"f{i}.scala", f"f{i}.scala  big.F{i}", f"def work{i} = typer implicit scope {i}", f"https://x/{i}") for i in range(30)]
            + [Chunk("code:n", "n.scala", "n.scala  big.N", "the needle method handles implicit shadowing in the typer", "https://x/n")])
        put("big", "issues", [Chunk("issues:issue:1", "issue:1", "o/r#1 shadowing bug", "implicit shadowing is broken", "https://x/i1", {"state": "closed", "number": 1}),
                              Chunk("issues:comment:5", "issue:1", "o/r#1 shadowing bug  (comment by a)", "me too, implicit shadowing", "https://x/c5", {"kind": "comment", "number": 1}),
                              Chunk("issues:issue:2", "issue:2", "o/r#2 other", "implicit shadowing, still open", "https://x/i2", {"state": "open", "number": 2})])
        put("small", "code", [Chunk("code:s", "s.java", "s.java  small.S", "needle for the small project: implicit shadowing in asm", "https://x/s")])
        self.idx = search.Index(self.cfg, "all")

    def tearDown(self):
        self.tmp.cleanup()

    def keys(self, **kw):
        return [h["key"] for h in search.hits(self.idx, kw.pop("q", "implicit shadowing needle"), embedder=FakeEmbedder(), **kw)]

    def test_merges_projects_and_reports_missing(self):
        self.assertEqual(self.idx.missing, ["later"])                               # no database yet: skipped, not an error
        ks = self.keys(k=10)
        self.assertTrue({"big/code", "small/code", "big/issues"} <= set(ks))
        h = search.hits(self.idx, "needle", k=3, embedder=FakeEmbedder())[0]
        self.assertEqual((h["project"], h["source"], h["label"]), ("big", "code", "code"))
        self.assertIn("bm25", h); self.assertIn("vec", h)

    def test_filters(self):
        self.assertEqual({k for k in self.keys(k=10, projects=["small"])}, {"small/code"})
        self.assertEqual({k for k in self.keys(k=10, sources=["issues"])}, {"big/issues"})            # a bare source id
        self.assertEqual({k for k in self.keys(k=10, sources=["small/code"])}, {"small/code"})        # project/source
        self.assertEqual(self.keys(k=10, mode="bm25", projects=["nope"]), [])

    def test_one_hit_per_document_and_open_only(self):
        hs = search.hits(self.idx, "implicit shadowing", k=20, sources=["issues"], embedder=FakeEmbedder())
        self.assertEqual(sorted(h["doc"] for h in hs), ["issue:1", "issue:2"])                        # the issue and its comment are one document
        self.assertEqual({h["doc"]: h["state"] for h in hs}, {"issue:1": "closed", "issue:2": "open"})
        hs = search.hits(self.idx, "implicit shadowing", k=20, sources=["issues"], open_only=True, embedder=FakeEmbedder())
        self.assertEqual([h["doc"] for h in hs], ["issue:2"])                                         # the comment inherited "closed" from its issue
        hs = search.hits(self.idx, "implicit shadowing", k=20, sources=["issues"], open_only=True, mode="vec", embedder=FakeEmbedder())
        self.assertEqual([h["doc"] for h in hs], ["issue:2"])                                         # same through the vector path

    def test_vector_scores_merge_across_projects(self):
        keys, detail = search.search(self.idx, "needle small project asm", k=5, mode="vec", embedder=FakeEmbedder())
        self.assertEqual(keys[0][0], "small")                                                         # best cosine wins regardless of project
        self.assertEqual(detail[keys[0]]["vec"], 1)

    def test_rerank_sees_each_projects_best_hit(self):
        rr = FakeReranker()
        hs = search.hits(self.idx, "typer implicit scope", k=3, embedder=FakeEmbedder(), reranker=rr, pool_docs=3)
        self.assertTrue(any("small project" in d for d in rr.seen))                                   # the small project's hit was added to the 3
        self.assertIn("rerank", hs[0])
        self.assertIn("needle", hs[0]["text"])                                                        # the reranker's pick comes first

    def test_universe_selection(self):
        self.assertEqual({h["project"] for h in search.hits(search.Index(self.cfg, "small-only"), "needle", k=10, embedder=FakeEmbedder())}, {"small"})
        self.assertEqual(search.Index(self.cfg).universe.id, "all")                                   # the default universe


if __name__ == "__main__":
    unittest.main()
