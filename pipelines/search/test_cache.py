#!/usr/bin/env python3
"""Score and query-embedding caches: a repeat costs no model call, a changed document or query misses, the table is pruned."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(__file__))
from cache import CachedReranker, CachedEmbedder


class Counting:
    name = "m"

    def __init__(self):
        self.calls = []

    def scores(self, q, docs):
        self.calls.append(list(docs))
        return [float(len(d)) for d in docs]

    def query(self, q):
        self.calls.append(q)
        return [len(q)]


class CacheTests(unittest.TestCase):
    def test_reranker_scores_are_reused_per_document(self):
        with tempfile.TemporaryDirectory() as d:
            inner = Counting()
            rr = CachedReranker(inner, os.path.join(d, "c.db"))
            self.assertEqual(rr.scores("q", ["a", "bb"]), [1.0, 2.0])
            self.assertEqual((rr.hits, rr.misses), (0, 2))
            self.assertEqual(rr.scores("q", ["bb", "ccc", "a"]), [2.0, 3.0, 1.0])
            self.assertEqual((rr.hits, rr.misses, inner.calls[-1]), (2, 1, ["ccc"]))         # only the new document was scored
            rr.scores("other query", ["a"])
            self.assertEqual(inner.calls[-1], ["a"])                                          # the query is part of the key
            self.assertEqual(CachedReranker(inner, os.path.join(d, "c.db")).scores("q", ["a"]), [1.0])   # persisted: a new process hits too
            self.assertEqual(len(inner.calls), 3)

    def test_prune_keeps_the_table_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            rr = CachedReranker(Counting(), os.path.join(d, "c.db"), max_entries=20)
            for i in range(10):
                rr.scores(f"q{i}", [f"doc{j}" for j in range(5)])
            self.assertLessEqual(rr.size(), 20)

    def test_query_embeddings_lru(self):
        inner = Counting()
        e = CachedEmbedder(inner, size=2)
        e.query("a"); e.query("a"); e.query("b"); e.query("c"); e.query("a")
        self.assertEqual(inner.calls, ["a", "b", "c", "a"])                                  # a was evicted by c, then recomputed
        self.assertEqual(e.name, "m")                                                          # other attributes pass through


if __name__ == "__main__":
    unittest.main()
