#!/usr/bin/env python3
"""Using links in search (LINKS.md step 3): naming a document (`LinkDB.resolve`), relations seen from both ends, `links` on every hit and in its one-line
summary, the `linked_to` / `link_type` / `has_link` restrictions applied before ranking (keyword and vector), a reference spelled out in the query putting
that document and its neighbours first, the story of a document, and everything degrading to plain search without a links database. Same fixture as test_links."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(__file__))
import search
from linkdb import LinkDB, NAMES
import test_links as TL
from test_links import SHA, SHA2


class Base(unittest.TestCase):
    build, tearDown = TL.LinksTests.build, TL.LinksTests.tearDown

    def setUp(self):
        TL.LinksTests.setUp(self)
        self.build()
        self.idx = search.Index(self.cfg)
        self.addCleanup(self.idx.close)

    def hits(self, q="crash fixes", link=None, **kw):
        lf, _ = search.link_filter(self.idx, **link) if link else (None, {})
        return search.hits(self.idx, q, k=20, mode="bm25", link_filter=lf, **kw)

    def ids(self, rows):
        return [h["doc"] for h in rows]


class LinkDBTests(Base):
    def test_resolve_forms(self):
        ld = self.idx.links
        self.assertEqual([n["id"] for n in ld.resolve("o/r#1")], ["o/r#1"])
        self.assertEqual([n["id"] for n in ld.resolve("#2")], ["o/r#2"])                       # bare: every indexed issue or PR with that number
        self.assertEqual([(n["id"], n["indexed"]) for n in ld.resolve("SI-9")], [("scala/bug#9", False)])   # a dangling node: known, but there is no document
        self.assertEqual([n["id"] for n in ld.resolve(SHA[:7])], [f"commit:o/r@{SHA}"])
        self.assertEqual([n["id"] for n in ld.resolve(f"https://github.com/o/r/commit/{SHA}")], [f"commit:o/r@{SHA}"])
        self.assertEqual([n["id"] for n in ld.resolve("old/name#3")], ["o/r#3"])               # alias
        got = ld.resolve("p/issues:issue:1")                                                   # the `ref` of a search hit
        self.assertEqual([n["id"] for n in got], ["o/r#1"])
        self.assertEqual(got[0]["get_ref"], "p/issues:issue:1")
        self.assertEqual(ld.resolve("nothing here"), [])

    def test_relations_are_seen_from_both_ends(self):
        ld = self.idx.links
        rels = {(r["rel"], r["node"]["id"]) for r in ld.neighbours("o/r#1")}
        self.assertIn(("closed_by", "o/r#2"), rels)
        self.assertIn(("closed_by", f"commit:o/r@{SHA}"), rels)
        self.assertIn(("shipped_in", "release:o/r@v1.0"), rels)
        self.assertIn(("mentioned_by", "o/r#3"), rels)
        self.assertEqual({r["rel"] for r in ld.neighbours("o/r#2", ["closes"])}, {"closes"})
        self.assertEqual(ld.neighbours("o/r#1", ["closes"]), [])                               # the relation `closes` is only the outgoing one; #1 closes nothing...
        self.assertEqual({r["rel"] for r in ld.neighbours("o/r#1", ["closed_by"])}, {"closed_by"})
        self.assertEqual({r["rel"] for r in ld.neighbours("o/r#1", ["mentions"])}, {"mentions"})            
        self.assertEqual({r["rel"] for r in ld.neighbours("o/r#3", ["mentions"])}, {"mentions"})
        with self.assertRaises(ValueError):
            ld.neighbours("o/r#1", ["nonsense"])

    def test_story_is_in_time_order_and_reaches_the_release_through_the_fix(self):
        s = self.idx.links.story("o/r#1")
        ids = [x["node"]["id"] for x in s]
        self.assertIn("o/r#1", ids); self.assertIn("o/r#2", ids)
        self.assertIn("release:o/r@v1.0", ids)
        whens = [x["when"] or "9999" for x in s]
        self.assertEqual(whens, sorted(whens))
        self.assertTrue(all(x["via"] is None for x in s if x["node"]["id"] == "o/r#1"))
        self.assertEqual(next(x["via"]["rel"] for x in s if x["node"]["id"] == "o/r#2"), "closed_by")


class SearchWithLinks(Base):
    def test_hits_carry_links_and_the_line_says_them(self):
        h = next(h for h in self.hits() if h["doc"] == "issue:1")
        self.assertEqual(h["links"]["counts"]["closed_by"], 2)
        self.assertTrue({"closed_by"} <= {t["rel"] for t in h["links"]["top"]})
        self.assertIn("closed by", h["line"])
        self.assertIn("shipped in release v1.0", h["line"])
        self.assertTrue(all(t["get_ref"] or not t["indexed"] for t in h["links"]["top"]))

    def test_linked_to_restricts_before_ranking(self):
        rows = self.hits(link={"linked_to": "o/r#1"})
        self.assertEqual(set(self.ids(rows)), {"issue:2", "issue:3", f"commit:{SHA}", "release:v1.0"})   # PR 2, issue 3, the commit and the release notes name #1; not #1 itself
        only = self.hits(link={"linked_to": "o/r#1", "link_type": ["closed_by"]})
        self.assertEqual(set(self.ids(only)), {"issue:2", f"commit:{SHA}"})
        self.assertEqual(self.hits(link={"linked_to": "o/r#1", "link_type": ["closes"]}), [])  # #1 closes nothing: the relation is the outgoing end only
        both = self.hits(link={"linked_to": "o/r#1", "link_type": ["closes", "closed_by"]})    # name both ends for either direction
        self.assertEqual(set(self.ids(both)), {"issue:2", f"commit:{SHA}"})
        self.assertEqual(self.hits(link={"linked_to": "no-such-thing"}), [])

    def test_has_link_and_its_negation(self):
        fixed = self.hits(link={"has_link": ["closed_by"]}, kinds=["issue"])
        self.assertEqual(set(self.ids(fixed)), {"issue:1"})
        unfixed = self.hits(link={"has_link": ["no_closed_by"]}, kinds=["issue", "pr"])
        self.assertEqual(set(self.ids(unfixed)), {"issue:3"})                                  # (PR 2 is closed_by the second commit's "Fixes old/name#2")
        both = self.hits(link={"has_link": ["shipped_in", "no_closed_by"]}, kinds=["issue", "pr"])
        self.assertEqual(set(self.ids(both)), {"issue:3"})
        with self.assertRaises(ValueError):
            search.link_filter(self.idx, has_link=["closed_bye"])
        with self.assertRaises(ValueError):
            search.link_filter(self.idx, link_type=["closes"])                                 # link_type without linked_to

    def test_a_reference_in_the_query_puts_that_document_and_its_neighbours_first(self):
        rows = search.hits(self.idx, "o/r#1", k=10, mode="bm25")
        self.assertEqual(rows[0]["doc"], "issue:1")
        self.assertEqual(rows[0]["pin"], 1)
        self.assertIn("issue:2", [h["doc"] for h in rows[:4]])                                # what links to it follows
        off = search.hits(self.idx, "o/r#1", k=10, mode="bm25", refs_in_query=False)
        self.assertFalse(any("pin" in h for h in off))
        self.assertEqual(search.hits(self.idx, "crash and SI-9", k=5, mode="bm25")[0]["doc"] in ("issue:1", "issue:2"), True)

    def test_filters_and_pins_respect_each_other(self):
        rows = search.hits(self.idx, "o/r#1", k=10, mode="bm25", kinds=["pr"])
        self.assertEqual({h["kind"] for h in rows}, {"pr"})

    def test_without_a_links_database_search_is_plain(self):
        os.remove(self.root / "data" / "links" / "u.db")
        idx = search.Index(self.cfg)
        try:
            rows = search.hits(idx, "crash", k=5, mode="bm25")
            self.assertTrue(rows and not any("links" in h for h in rows))
            lf, _ = search.link_filter(idx, has_link=["closed_by"])
            self.assertEqual(search.hits(idx, "crash", k=5, mode="bm25", link_filter=lf), [])    # asked for linked documents, there are none
        finally:
            idx.close()


if __name__ == "__main__":
    unittest.main()
