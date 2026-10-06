#!/usr/bin/env python3
"""Duplicate pairs and topic clusters over issue and PR vectors (run with the .venv-jev python). A fake embedder makes identical text identical
vectors. Checks: only issues and PRs are items (comments and continuation chunks are not), close pairs are found and unrelated ones are not,
clusters separate topics and are named by their title words, a run is skipped when nothing changed and redone when an item is added or the
parameters change, and the refresh plans the phase."""
import json, os, sys, tempfile, unittest, zlib
from pathlib import Path
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
import config, embed, neighbours, refresh
from store import Store, Chunk
from test_config import write, proj, uni, GH, GIT


class FakeEmbedder:
    name, dev = "fake", "cpu"

    def docs(self, texts):
        out = []
        for t in texts:
            v = np.zeros(64, dtype=np.float32)
            for w in t.lower().split():
                v[zlib.crc32(w.encode()) % 64] += 1
            out.append(v / (np.linalg.norm(v) or 1))
        return np.vstack(out)


def issue(n, title, body, state="open", kind="issue", created="2024-03-01T00:00:00Z"):
    cid = f"issues:{'issue' if kind == 'issue' else 'pr'}:{n}"
    return Chunk(cid, f"{kind}:{n}", f"o/r#{n} {title}", body, f"https://github.com/o/r/issues/{n}",
                 {"kind": kind, "state": state, "number": n, "created": created, "author": "a"})


TYPER = "implicit shadowing typer crash while resolving implicit scope"
ASM = "classfile writer asm frames stack map computation fails"


class NeighboursTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write(self.root, search={"data_dir": str(self.root / "data"), "embedder": {"model": "fake"}, "neighbours": {"clusters": 2, "min_similarity": 0.8}},
              projects=[proj("p", {**GH, "id": "issues"})], universes=[uni("u", "p", default=True)])
        self.cfg = config.load(self.root)
        self.add([issue(1, "typer crash", TYPER), issue(2, "typer crash again", TYPER, state="closed"), issue(3, "implicit scope shadowing", TYPER + " bug", kind="pr", state="merged"),
                  issue(10, "asm frames", ASM), issue(11, "asm frames copy", ASM, state="closed"), issue(12, "stack map frames asm", ASM + " again"),
                  Chunk("issues:comment:5", "issue:1", "o/r#1 typer crash  (comment by a)", TYPER, "https://x/c", {"kind": "comment", "number": 1}),
                  Chunk("issues:issue:1~1", "issue:1", "o/r#1 typer crash", TYPER, "https://x/1", {"kind": "issue", "state": "open", "number": 1})])

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, chunks):
        st = Store(self.cfg.project_db("p"))
        st.apply("issues", chunks)
        st.commit()
        embed.fill(st, FakeEmbedder(), source="issues", log=lambda *_: None)
        st.db.execute("UPDATE vec SET model = 'fake'")
        st.commit()

    def db(self):
        import sqlite3
        return sqlite3.connect(neighbours.db_path(self.cfg, "u"))

    def test_items_are_issues_and_prs_only(self):
        s = neighbours.compute(self.cfg, "u")
        self.assertEqual(s["items"], 6)
        rows = self.db().execute("SELECT number, kind, state FROM items ORDER BY number").fetchall()
        self.assertEqual(rows, [(1, "issue", "open"), (2, "issue", "closed"), (3, "pr", "merged"), (10, "issue", "open"), (11, "issue", "closed"), (12, "issue", "open")])

    def test_identical_text_pairs_and_unrelated_does_not(self):
        neighbours.compute(self.cfg, "u")
        con = self.db()
        num = dict(con.execute("SELECT idx, number FROM items"))
        pairs = {tuple(sorted((num[a], num[b]))): sim for a, b, sim in con.execute("SELECT a, b, sim FROM pairs")}
        self.assertGreater(pairs[(1, 2)], 0.9)
        self.assertGreater(pairs[(10, 11)], 0.9)
        self.assertFalse(any((a in (1, 2, 3)) != (b in (1, 2, 3)) for a, b in pairs))
        self.assertTrue(all(sim >= 0.8 for sim in pairs.values()))
        self.assertTrue(all(a < b for a, b in con.execute("SELECT a, b FROM pairs")))

    def test_clusters_separate_topics_and_are_named_by_title_words(self):
        self.add([issue(100 + i, f"typer implicit {i}", TYPER) for i in range(7)] + [issue(200 + i, f"asm frames {i}", ASM) for i in range(7)])
        neighbours.compute(self.cfg, "u")
        con = self.db()
        by = {}
        for title, cl in con.execute("SELECT title, cluster FROM items"):
            by.setdefault(cl, set()).add("asm" if "asm" in title or "frames" in title else "typer")
        self.assertEqual(len(by), 2)
        self.assertTrue(all(len(topics) == 1 for topics in by.values()), by)
        labels = [r[0] for r in con.execute("SELECT label FROM clusters")]
        self.assertTrue(any("typer" in x for x in labels) and any("asm" in x for x in labels), labels)
        for k, label, samples in con.execute("SELECT k, label, samples FROM clusters"):
            self.assertEqual(len(json.loads(samples)), 8)

    def test_at_most_one_cluster_per_ten_items(self):
        neighbours.compute(self.cfg, "u")
        self.assertEqual(self.db().execute("SELECT count(*) FROM clusters").fetchone()[0], 1)

    def test_skipped_when_unchanged_and_redone_on_change(self):
        self.assertIsNotNone(neighbours.compute(self.cfg, "u"))
        first = self.db().execute("SELECT v FROM meta WHERE k = 'generated'").fetchone()
        self.assertIsNone(neighbours.compute(self.cfg, "u"))
        self.assertEqual(self.db().execute("SELECT v FROM meta WHERE k = 'generated'").fetchone(), first)
        self.assertIsNotNone(neighbours.compute(self.cfg, "u", force=True))
        self.add([issue(13, "another asm one", ASM)])
        s = neighbours.compute(self.cfg, "u")
        self.assertEqual(s["items"], 7)
        self.add([issue(13, "another asm one", ASM, state="closed")])
        self.assertIsNotNone(neighbours.compute(self.cfg, "u"))

    def test_isolation_and_centre_scores_rank_an_off_topic_item_first(self):
        self.add([issue(50, "banana smoothie", "quantum banana smoothie recipe with oat milk")])
        neighbours.compute(self.cfg, "u")
        con = self.db()
        rows = con.execute("SELECT number, iso, ctr FROM items ORDER BY iso").fetchall()
        self.assertEqual(rows[0][0], 50)
        self.assertLess(rows[0][1], 0.5)
        self.assertGreater(rows[1][1], 0.8)
        self.assertTrue(all(0 <= iso <= 1 and 0 <= ctr <= 1 for _, iso, ctr in rows))
        by_ctr = con.execute("SELECT number FROM items ORDER BY ctr").fetchall()
        self.assertEqual(by_ctr[0][0], 50)

    def test_a_database_from_before_the_scores_is_recomputed(self):
        neighbours.compute(self.cfg, "u")
        con = self.db()
        con.execute("UPDATE meta SET v = '1' WHERE k = 'version'")
        con.commit(); con.close()
        self.assertIsNotNone(neighbours.compute(self.cfg, "u"))
        self.assertEqual(self.db().execute("SELECT v FROM meta WHERE k = 'version'").fetchone()[0], neighbours.VERSION)

    def test_changed_parameters_recompute(self):
        neighbours.compute(self.cfg, "u")
        write(self.root, search={"data_dir": str(self.root / "data"), "embedder": {"model": "fake"}, "neighbours": {"clusters": 3, "min_similarity": 0.8}},
              projects=[proj("p", {**GH, "id": "issues"})], universes=[uni("u", "p", default=True)])
        self.assertIsNotNone(neighbours.compute(config.load(self.root), "u"))

    def test_an_empty_universe_still_produces_a_database(self):
        write(self.root, search={"data_dir": str(self.root / "empty"), "embedder": {"model": "fake"}}, projects=[proj("p", {**GH, "id": "issues"})], universes=[uni("u", "p", default=True)])
        self.assertEqual(neighbours.compute(config.load(self.root), "u")["items"], 0)

    def test_kmeans_is_deterministic_and_handles_more_clusters_than_points(self):
        X = np.vstack([np.eye(8)[i % 2] + 0.01 * np.random.default_rng(i).standard_normal(8) for i in range(20)]).astype(np.float32)
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        a, _ = neighbours.kmeans(X, 2)
        b, _ = neighbours.kmeans(X, 2)
        self.assertEqual(a.tolist(), b.tolist())
        self.assertEqual(len(set(a[::2])), 1)
        self.assertEqual(len(neighbours.kmeans(X[:3], 10)[0]), 3)

    def test_refresh_plans_the_phase_after_embed_and_can_disable_it(self):
        steps = refresh.plan(self.cfg, "u", [], {"only": set(), "skip": set()}, {}, 0)
        names = [p for p, _, _ in steps]
        self.assertEqual(names.index("neighbours"), names.index("embed") + 1)
        self.assertTrue(dict((p, on) for p, on, _ in steps)["neighbours"])
        write(self.root, search={"data_dir": str(self.root / "data"), "neighbours": {"enabled": False}}, projects=[proj("p", {**GH, "id": "issues"})], universes=[uni("u", "p", default=True)])
        off = config.load(self.root)
        self.assertFalse(dict((p, on) for p, on, _ in refresh.plan(off, "u", [], {"only": set(), "skip": set()}, {}, 0))["neighbours"])
        self.assertTrue(dict((p, on) for p, on, _ in refresh.plan(off, "u", [], {"only": {"neighbours"}, "skip": set()}, {}, 0))["neighbours"])


class AllKindsTests(unittest.TestCase):
    """Every kind of document is an item: commits, releases (their header), tags, forum topics (the opening post), files (the mean of their chunks)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write(self.root, search={"data_dir": str(self.root / "data"), "embedder": {"model": "fake"}, "neighbours": {"clusters": 2, "min_similarity": 0.8}},
              projects=[proj("p", {**GH, "id": "issues", "include": ["issues", "prs", "comments"]}, {"id": "commits", "type": "git_log", "label": "c", "repo": "o/r", "ref": "main"},
                             {"id": "rel", "type": "github_releases", "label": "r", "repo": "o/r", "tag_messages": True}, {**GIT, "id": "code"}),
                        proj("f", {"id": "forum", "type": "discourse", "label": "f", "site": "forum.example.org"})],
              universes=[uni("u", "p", "f", default=True)])
        self.cfg = config.load(self.root)
        st = Store(self.cfg.project_db("p"))
        st.apply("issues", [issue(1, "typer crash", TYPER), issue(2, "fix typer crash", TYPER, kind="pr", state="merged"),
                            Chunk("issues:comment:9", "issue:1", "o/r#1 typer crash  (comment by a)", TYPER, "https://x/c", {"kind": "comment", "number": 1})])
        st.apply("commits", [Chunk("commits:commit:" + "a" * 40, "commit:" + "a" * 40, "o/r commit aaaaaaaa fix typer crash", TYPER, "https://x/commit",
                                   {"kind": "commit", "sha": "a" * 40, "author_name": "A Person", "created": "2024-03-02T00:00:00Z"}),
                             Chunk("commits:commit:" + "b" * 40, "commit:" + "b" * 40, "o/r commit bbbbbbbb asm frames", ASM, "https://x/commit2",
                                   {"kind": "commit", "sha": "b" * 40, "created": "2024-03-03T00:00:00Z"})])
        st.apply("rel", [Chunk("rel:release:v1:header", "release:v1", "o/r release v1", "typer fixes " + TYPER, "https://x/r", {"kind": "release", "tag": "v1", "published": "2024-04-01"}),
                         Chunk("rel:release:v1:notes", "release:v1", "o/r release v1  Notes", ASM, "https://x/r", {"kind": "release", "tag": "v1", "published": "2024-04-01"}),
                         Chunk("rel:tag:v0", "tag:v0", "o/r tag v0", "first tag " + ASM, "https://x/t", {"kind": "tag", "tag": "v0", "published": "2023-01-01"})])
        st.apply("code", [Chunk("code:src/Typer.scala:a", "src/Typer.scala", "src/Typer.scala  typer  A", TYPER, "https://x/Typer.scala#L1", {}),
                          Chunk("code:src/Typer.scala:b", "src/Typer.scala", "src/Typer.scala  typer  B", "implicit scope resolution", "https://x/Typer.scala#L9", {})])
        st.commit()
        embed.fill(st, FakeEmbedder(), log=lambda *_: None)
        st.db.execute("UPDATE vec SET model = 'fake'"); st.commit()
        st = Store(self.cfg.project_db("f"))
        st.apply("forum", [Chunk("forum:post:1", "topic:7", "Pre-SIP: frames", ASM, "https://forum.example.org/t/x/7", {"kind": "topic", "topic": 7, "author": "b", "created": "2024-05-01T00:00:00Z"}),
                           Chunk("forum:post:2", "topic:7", "Pre-SIP: frames  (reply #2 by c)", TYPER, "https://forum.example.org/t/x/7/2", {"kind": "post", "topic": 7})])
        st.commit()
        embed.fill(st, FakeEmbedder(), log=lambda *_: None)
        st.db.execute("UPDATE vec SET model = 'fake'"); st.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def con(self):
        import sqlite3
        return sqlite3.connect(neighbours.db_path(self.cfg, "u"))

    def test_one_item_per_document_of_every_kind(self):
        s = neighbours.compute(self.cfg, "u")
        rows = {r[0]: r[1:] for r in self.con().execute("SELECT id, kind, title, url, author, created FROM items")}
        self.assertEqual(sorted(k for k, *_ in rows.values()), ["commit", "commit", "file", "issue", "pr", "release", "tag", "topic"])
        self.assertEqual(s["items"], 8)
        self.assertEqual(rows["code:src/Typer.scala:a"][1:3], ("src/Typer.scala", "https://x/Typer.scala"))          # a file: its path, the URL without a line
        self.assertEqual(rows["commits:commit:" + "a" * 40][3], "A Person")                                         # a commit's git name when there is no handle
        self.assertEqual(rows["rel:release:v1:header"][4], "2024-04-01")                                             # a release dates by when it was published
        self.assertNotIn("forum:post:2", rows)                                                                      # replies and comments belong to their thread
        self.assertNotIn("rel:release:v1:notes", rows)

    def test_isolation_is_within_a_group_and_pairs_are_issues_and_prs(self):
        neighbours.compute(self.cfg, "u")
        con = self.con()
        iso = dict(con.execute("SELECT kind, iso FROM items WHERE kind IN ('pr', 'topic')"))
        self.assertGreater(iso["pr"], 0.9)                                    # its issue says the same: not isolated
        self.assertEqual(iso["topic"], 0.0)                                   # the only forum topic: nothing of its group to be near
        kinds = {k for a, b in con.execute("SELECT a, b FROM pairs").fetchall() for (k,) in con.execute("SELECT kind FROM items WHERE idx IN (?, ?)", (a, b))}
        self.assertEqual(kinds, {"issue", "pr"})                              # the identical commit is no duplicate candidate


if __name__ == "__main__":
    unittest.main()
