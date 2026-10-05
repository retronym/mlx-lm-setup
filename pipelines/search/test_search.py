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
        put("big", "issues", [Chunk("issues:issue:1", "issue:1", "o/r#1 shadowing bug", "implicit shadowing is broken", "https://x/i1", {"kind": "issue", "state": "closed", "number": 1}),
                              Chunk("issues:comment:5", "issue:1", "o/r#1 shadowing bug  (comment by a)", "me too, implicit shadowing", "https://x/c5", {"kind": "comment", "number": 1}),
                              Chunk("issues:issue:2", "issue:2", "o/r#2 other", "implicit shadowing, still open", "https://x/i2", {"kind": "issue", "state": "open", "number": 2})])
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
        self.assertEqual(h["ref"], "big/code:n")
        self.assertEqual(h["line"], "n.scala big.N · file · big/code · https://x/n")                  # state-less, authorless file chunk
        iss = search.hits(self.idx, "still open", k=3, sources=["issues"], embedder=FakeEmbedder())[0]
        self.assertTrue(iss["line"].startswith("[open] o/r#2 other · issue · big/issues"), iss["line"])                                                    # project/chunk id: what the get tool takes

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

    def test_kinds(self):
        for mode in ("bm25", "vec"):
            hs = search.hits(self.idx, "implicit shadowing", k=20, kinds=["comment"], mode=mode, embedder=FakeEmbedder())
            self.assertEqual([(h["doc"], h["kind"]) for h in hs], [("issue:1", "comment")], mode)         # the comment, not its issue's body
            hs = search.hits(self.idx, "implicit shadowing needle", k=50, kinds=["file"], mode=mode, embedder=FakeEmbedder())
            self.assertEqual({(h["key"], h["kind"]) for h in hs}, {("big/code", "file"), ("small/code", "file")}, mode)   # chunks of files carry no kind
        self.assertEqual(self.keys(k=10, kinds=["commit"]), [])
        self.assertEqual({k for k in self.keys(k=10, kinds=["file", "comment"], sources=["issues"])}, {"big/issues"})  # combines with sources

    def test_vector_scores_merge_across_projects(self):
        keys, detail = search.search(self.idx, "needle small project asm", k=5, mode="vec", embedder=FakeEmbedder())
        self.assertEqual(keys[0][0], "small")                                                         # best cosine wins regardless of project
        self.assertEqual(detail[keys[0]]["vec"], 1)

    def test_rerank_sees_each_projects_best_hit(self):
        rr = FakeReranker()
        hs = search.hits(self.idx, "typer implicit scope", k=3, embedder=FakeEmbedder(), reranker=rr, pool_docs=3, blend=None)
        self.assertTrue(any("small project" in d for d in rr.seen))                                   # the small project's hit was added to the 3
        self.assertIn("rerank", hs[0])
        self.assertIn("needle", hs[0]["text"])                                                        # the reranker's pick comes first

    def test_blend_protects_the_retrieval_order_and_explain_shows_the_arithmetic(self):
        rr = FakeReranker()
        plain = search.hits(self.idx, "typer implicit scope", k=3, embedder=FakeEmbedder())
        blended = search.hits(self.idx, "typer implicit scope", k=3, embedder=FakeEmbedder(), reranker=rr, pool_docs=30, explain=True)
        self.assertEqual(blended[0]["doc"], plain[0]["doc"])                                          # rank 1 stays: 0.75 retrieval beats one reranker vote
        x = blended[0]["explain"]
        self.assertEqual((x["fused_rank"], x["weight"], x["retrieval"]), (1, 0.75, 1.0))
        self.assertAlmostEqual(x["final"], 0.75 * 1.0 + 0.25 * blended[0]["rerank"], places=3)
        alone = search.hits(self.idx, "typer implicit scope", k=30, embedder=FakeEmbedder(), reranker=rr, pool_docs=30, blend=None)
        self.assertIn("needle", alone[0]["text"])                                                     # the reranker alone puts its favourite first
        self.assertNotIn("explain", search.hits(self.idx, "needle", k=3, embedder=FakeEmbedder())[0])

    def test_fusion_top_bonus_and_blend_weights(self):
        a, b, c = ("p", 1), ("p", 2), ("p", 3)
        self.assertEqual(search.fuse([[a, b], [c, b]], top_bonus=()), [b, a, c])                      # plain RRF: b is second in one list and second in the other
        sc = search.fuse_scores([[a, b], [c, b]], top_bonus=(0.05, 0.02))
        self.assertEqual((sc[a][1], sc[c][1], sc[b][1]), (0.05, 0.05, 0.02))                          # a top hit of ANY list gets the bonus
        self.assertEqual(search.fuse([[a, b], [c, b]], top_bonus=(0.05, 0.02))[:2], [a, c] if sc[a][0] >= sc[c][0] else [c, a])
        rows = [[3, 0.75], [10, 0.6], [1000, 0.4]]
        self.assertEqual([search.blend_weight(r, rows) for r in (1, 3, 4, 10, 11, 5000)], [0.75, 0.75, 0.6, 0.6, 0.4, 0.4])

    def test_exact_top_hit_survives_fuzzy_neighbours(self):
        keys, _ = search.search(self.idx, "needle", k=3, mode="bm25", embedder=FakeEmbedder())
        self.assertEqual(self.idx.stores[keys[0][0]].db.execute("SELECT id FROM chunks WHERE rowid=?", (keys[0][1],)).fetchone()[0] in ("code:n", "code:s"), True)

    def test_hits_say_who_and_when_with_fallbacks_for_older_chunks(self):
        st = Store(self.cfg.project_db("big"))
        st.apply("issues", [
            Chunk("issues:issue:40", "issue:40", "o/r#40 who wrote this", "zebra quagga thread body", "https://x/i40",
                  {"kind": "pr", "state": "open", "number": 40, "updated": "2026-03-01T00:00:00Z", "created": "2026-01-01T00:00:00Z", "author": "alice"}),
            Chunk("issues:comment:41", "issue:40", "o/r#40 who wrote this  (comment by bob)", "zebra quagga comment that is the best hit", "https://x/c41",
                  {"kind": "comment", "number": 40, "updated": "2026-03-02T00:00:00Z"}),                                    # an older comment: no author or created in its metadata
            Chunk("issues:review:42", "issue:40", "o/r#40 who wrote this  (review comment on a/b.scala by carol[bot])", "zebra quagga review remark", "https://x/r42",
                  {"kind": "review", "number": 40, "updated": "2026-03-03T00:00:00Z", "created": "2026-03-03T00:00:00Z", "author": "carol[bot]"})])
        st.apply("code", [Chunk("code:commit:abc", "commit:abc", "o/r commit abcd1234 quokka fix", "quokka fix in the typer", "https://x/abc",
                                {"kind": "commit", "author": "Dev One", "updated": "2026-02-01T00:00:00Z"}),               # written before handles: `author` is the git name
                          Chunk("code:commit:def", "commit:def", "o/r commit def56789 quokka again", "quokka again in the typer", "https://x/def",
                                {"kind": "commit", "author": "octocat", "author_name": "The Octocat", "updated": "2026-02-02T00:00:00Z", "created": "2026-02-02T00:00:00Z"}),
                          Chunk("code:release:v1", "release:v1", "o/r release v1", "quokka release", "https://x/v1", {"kind": "release", "published": "2026-01-15"})])
        st.commit(); embed.fill(st, FakeEmbedder(), log=lambda *_: None)
        idx = search.Index(self.cfg, "all")
        by_doc = {h["doc"]: h for h in search.hits(idx, "best hit", k=10, mode="bm25", sources=["big/issues"])}
        h = by_doc["issue:40"]                                                                                         # one hit per thread: the comment won
        self.assertEqual((h["kind"], h["author"]), ("comment", "bob"))                                                 # author recovered from the title
        self.assertEqual(h["thread"], {"number": 40, "kind": "pr", "author": "alice", "created": "2026-01-01T00:00:00Z"})
        self.assertEqual(h["updated"], "2026-03-02T00:00:00Z")
        commits = {h["doc"]: h for h in search.hits(idx, "quokka", k=10, mode="bm25", sources=["big/code"])}
        self.assertEqual((commits["commit:abc"]["author"], commits["commit:abc"]["author_name"], commits["commit:abc"]["created"]), (None, "Dev One", "2026-02-01T00:00:00Z"))
        self.assertEqual((commits["commit:def"]["author"], commits["commit:def"]["author_name"]), ("octocat", "The Octocat"))
        rel = commits["release:v1"]
        self.assertEqual((rel["created"], rel["updated"]), ("2026-01-15", "2026-01-15"))                              # a release's time falls back to its tag date

    def test_close_releases_the_databases(self):
        import sqlite3
        idx = search.Index(self.cfg, "all")
        idx.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            idx.stores["big"].db.execute("SELECT 1")                                                  # a per-request Index must not leave connections to the GC

    def test_universe_selection(self):
        self.assertEqual({h["project"] for h in search.hits(search.Index(self.cfg, "small-only"), "needle", k=10, embedder=FakeEmbedder())}, {"small"})
        self.assertEqual(search.Index(self.cfg).universe.id, "all")                                   # the default universe


if __name__ == "__main__":
    unittest.main()
