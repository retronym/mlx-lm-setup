"""The duplicates and clusters views over a neighbours database (the schema pipelines/search/neighbours.py writes), through the gateway's
stdlib-only reader: pair and item filters, the either-item semantics for dates and repos, the old-import adjacency rule, templated titles,
paging, input validation and a universe with nothing computed yet."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from gateway import searchinfo
from gateway.catalog import parse
from gateway.core import ApiError, search_clusters, search_duplicates

SEARCH = Path(__file__).parents[2] / "pipelines" / "search"
YEAR = 365 * 86400


def day(seconds_ago):
    return time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - seconds_ago))


class View(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "cfg" / "projects").mkdir(parents=True); (root / "cfg" / "universes").mkdir()
        (root / "cfg" / "search.json").write_text(json.dumps({"data_dir": str(root / "data")}))
        for pid in ("x", "y"):
            (root / "cfg" / "projects" / f"{pid}.json").write_text(json.dumps({"id": pid, "title": pid, "sources": [
                {"id": "issues", "type": "github", "label": "i", "repo": "o/r", "include": ["issues"]}]}))
        (root / "cfg" / "universes" / "u.json").write_text(json.dumps({"id": "u", "title": "U", "projects": ["x", "y"], "default": True}))
        (root / "cfg" / "universes" / "empty.json").write_text(json.dumps({"id": "empty", "title": "E", "projects": ["x"]}))
        _, self.cfg = searchinfo.load_config(str(SEARCH), str(root / "cfg"))
        self.items = [
            # idx, project, kind, state, created, number, title, cluster
            (0, "x", "issue", "open", "2011-05-01T00:00:00Z", 100, "x#100 Crash in typer", 0),
            (1, "x", "issue", "closed", "2011-05-01T00:00:00Z", 101, "x#101 Crash in typer", 0),
            (2, "x", "issue", "open", "2012-01-01T00:00:00Z", 200, "x#200 Both open", 0),
            (3, "x", "issue", "open", "2012-01-01T00:00:00Z", 201, "x#201 Both open", 0),
            (4, "x", "issue", "open", day(YEAR // 2), 5000, "x#5000 Implicit scope broken", 1),
            (5, "y", "issue", "closed", "2020-02-02T00:00:00Z", 77, "y#77 Implicit scope broken", 1),
            (6, "x", "issue", "closed", "2019-01-01T00:00:00Z", 300, "x#300 Release 2.13.1", 2),
            (7, "x", "issue", "closed", "2019-06-01T00:00:00Z", 310, "x#310 Release 2.13.2", 2),
            (8, "x", "issue", "open", day(YEAR // 4), 6000, "x#6000 Lazy val deadlock", 2),
            (9, "y", "pr", "merged", day(YEAR // 3), 88, "y#88 Fix lazy val deadlock", 2),
            (10, "y", "pr", "closed", "2018-01-01T00:00:00Z", 90, "y#90 Fix scaladoc", 3),
            (11, "y", "pr", "merged", "2018-02-01T00:00:00Z", 95, "y#95 Fix scaladoc links", 3),
        ]
        pairs = [(0, 1, 0.99), (2, 3, 0.97), (4, 5, 0.95), (6, 7, 0.98), (8, 9, 0.92), (10, 11, 0.91), (4, 8, 0.82)]
        path = root / "data" / "neighbours" / "u.db"
        path.parent.mkdir(parents=True)
        con = sqlite3.connect(path)
        con.executescript("""CREATE TABLE items(idx INTEGER PRIMARY KEY, project TEXT, id TEXT, kind TEXT, state TEXT, created TEXT, number INTEGER, author TEXT, title TEXT, url TEXT, cluster INTEGER);
                             CREATE TABLE pairs(a INTEGER, b INTEGER, sim REAL, PRIMARY KEY(a, b));
                             CREATE TABLE clusters(k INTEGER PRIMARY KEY, label TEXT, samples TEXT);
                             CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);""")
        con.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?)", [(i, p, f"{p}:{n}", k, s, c, n, "a", t, f"https://github.com/o/{p}/issues/{n}", cl) for i, p, k, s, c, n, t, cl in self.items])
        con.executemany("INSERT INTO pairs VALUES(?,?,?)", pairs)
        con.executemany("INSERT INTO clusters VALUES(?,?,?)", [(0, "typer, crash", "[0, 1, 2]"), (1, "implicit, scope", "[4, 5]"), (2, "lazy, release", "[8, 9, 6]"), (3, "scaladoc", "[10]")])
        con.execute("INSERT INTO meta VALUES('generated', '1700000000')")
        con.commit(); con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def dup(self, **kw):
        return [(p["a"]["number"], p["b"]["number"]) for p in searchinfo.duplicates(self.cfg, "u", **kw)["pairs"]]

    def test_default_hides_adjacent_import_copies_and_templated_titles(self):
        self.assertEqual(self.dup(), [(200, 201), (5000, 77)])
        self.assertEqual(self.dup(templated=True), [(300, 310), (200, 201), (5000, 77)])

    def test_the_adjacency_rule_needs_a_closed_one_and_the_same_repo_and_kind(self):
        self.assertIn((100, 101), self.dup(adjacent=0))
        self.assertIn((200, 201), self.dup(adjacent=1))
        self.assertEqual(self.dup(adjacent=100, kind="any").count((5000, 77)), 1)

    def test_state_is_about_the_pair(self):
        self.assertEqual(self.dup(state="open", adjacent=0), [(100, 101), (200, 201), (5000, 77)])
        self.assertEqual(self.dup(state="closed", templated=True, adjacent=0, kind="any"), [(300, 310), (90, 95)])

    def test_kind_selects_issue_pairs_pr_pairs_or_mixed(self):
        self.assertEqual(self.dup(kind="pr"), [(90, 95)])
        self.assertEqual(self.dup(kind="mixed"), [(6000, 88)])
        self.assertEqual(self.dup(kind="any", min_sim=0.9), [(200, 201), (5000, 77), (6000, 88), (90, 95)])

    def test_dates_and_repos_match_if_either_item_does(self):
        self.assertEqual(self.dup(since="2024"), [(5000, 77)])
        self.assertEqual(self.dup(until="2013", adjacent=0), [(100, 101), (200, 201)])
        self.assertEqual(self.dup(since="2020-01", until="2020-03"), [(5000, 77)])
        self.assertEqual(self.dup(projects=["y"]), [(5000, 77)])
        self.assertEqual(self.dup(projects=["y"], kind="any", min_sim=0.9), [(5000, 77), (6000, 88), (90, 95)])

    def test_similarity_floor_total_and_paging(self):
        self.assertEqual(self.dup(min_sim=0.96), [(200, 201)])
        d = searchinfo.duplicates(self.cfg, "u", min_sim=0.8, kind="any", limit=2)
        self.assertEqual((d["total"], len(d["pairs"]), d["generated"]), (5, 2, 1700000000.0))
        later = searchinfo.duplicates(self.cfg, "u", min_sim=0.8, kind="any", limit=2, offset=2)
        self.assertEqual([p["sim"] for p in d["pairs"] + later["pairs"]], [0.97, 0.95, 0.92, 0.91][:len(d["pairs"] + later["pairs"])])

    def test_bad_input_is_a_value_error(self):
        for kw in ({"state": "maybe"}, {"kind": "commit"}, {"since": "last year"}, {"until": "2024-1"}):
            with self.assertRaises(ValueError):
                searchinfo.duplicates(self.cfg, "u", **kw)
            with self.assertRaises(ValueError):
                searchinfo.clusters(self.cfg, "u", **{k: v for k, v in kw.items() if k != "kind"} or {"kind": "mixed"})

    def test_a_universe_without_a_database_says_so(self):
        self.assertEqual(searchinfo.duplicates(self.cfg, "empty")["available"], False)
        self.assertEqual(searchinfo.clusters(self.cfg, "empty")["clusters"], [])
        with self.assertRaises(KeyError):
            searchinfo.duplicates(self.cfg, "nope")

    def test_clusters_count_items_under_the_filters(self):
        d = searchinfo.clusters(self.cfg, "u")
        by = {c["k"]: c for c in d["clusters"]}
        self.assertEqual((d["items"], [c["k"] for c in d["clusters"]][0]), (12, 0))
        self.assertEqual((by[0]["size"], by[0]["open"], by[2]["size"], by[2]["open"]), (4, 3, 4, 1))
        self.assertEqual(by[1]["projects"], {"x": 1, "y": 1})
        self.assertEqual([s["number"] for s in by[2]["samples"]], [6000, 88, 300])
        o = {c["k"]: c["size"] for c in searchinfo.clusters(self.cfg, "u", state="open")["clusters"]}
        self.assertEqual(o, {0: 3, 1: 1, 2: 1})
        c = {c["k"]: c["size"] for c in searchinfo.clusters(self.cfg, "u", state="closed")["clusters"]}
        self.assertEqual(c, {0: 1, 1: 1, 2: 3, 3: 2})
        self.assertEqual({c["k"]: c["size"] for c in searchinfo.clusters(self.cfg, "u", kind="pr")["clusters"]}, {2: 1, 3: 2})
        self.assertEqual({c["k"]: c["size"] for c in searchinfo.clusters(self.cfg, "u", since="2024")["clusters"]}, {1: 1, 2: 2})
        self.assertEqual({c["k"]: c["size"] for c in searchinfo.clusters(self.cfg, "u", projects=["y"])["clusters"]}, {1: 1, 2: 1, 3: 2})

    def test_trend_compares_the_recent_share_to_the_overall_share(self):
        d = searchinfo.clusters(self.cfg, "u")
        by = {c["k"]: c for c in d["clusters"]}
        self.assertEqual((by[0]["recent"], by[1]["recent"], by[2]["recent"], by[3]["recent"]), (0, 1, 2, 0))
        self.assertAlmostEqual(d["recent_share"], 3 / 12, places=3)
        self.assertEqual(by[2]["trend"], 2.0)                      # 2 of 4 recent against 3 of 12 overall
        self.assertEqual(by[0]["trend"], 0.0)

    def test_one_clusters_items_newest_first_with_paging(self):
        d = searchinfo.clusters(self.cfg, "u", cluster=2, limit=3)
        self.assertEqual((d["total"], [i["number"] for i in d["items"]]), (4, [6000, 88, 310]))
        d = searchinfo.clusters(self.cfg, "u", cluster=2, limit=3, offset=3)
        self.assertEqual([i["number"] for i in d["items"]], [300])
        d = searchinfo.clusters(self.cfg, "u", cluster=2, state="open")
        self.assertEqual([i["number"] for i in d["items"]], [6000])

    def test_core_wrappers_turn_bad_input_and_unknown_universes_into_api_errors(self):
        cat = parse({"backends": {"s": {"adapter": "search", "python": "py", "index_dir": str(SEARCH), "config_dir": str(Path(self.tmp.name) / "cfg"), "est_mem_gb": 1}}}, Path("/base"))
        d = search_duplicates(cat, universe="u", kind="any", min_sim=0.9)
        self.assertEqual((d["backend"], d["available"], d["total"]), ("s", True, 4))
        self.assertEqual(search_clusters(cat, universe="u", cluster=3)["total"], 2)
        for fn, kw, status in ((search_duplicates, {"universe": "u", "state": "maybe"}, 400), (search_clusters, {"universe": "u", "since": "x"}, 400),
                               (search_duplicates, {"universe": "nope"}, 404)):
            with self.assertRaises(ApiError) as cm:
                fn(cat, **kw)
            self.assertEqual(cm.exception.status, status)


if __name__ == "__main__":
    unittest.main()
