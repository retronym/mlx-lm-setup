"""`links` (core.search_links, POST /api/search/links, the MCP tool's engine): a document's neighbourhood and story read from the links database the
refresh builds, with a real (tiny) index; errors and missing pieces are answers, not crashes."""
import json, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipelines" / "search"))
from gateway.catalog import parse  # noqa: E402
from gateway.core import ApiError, search_links  # noqa: E402
import config, links  # noqa: E402
from store import Store, Chunk  # noqa: E402

SEARCH = ROOT / "pipelines" / "search"


def issue(n, title, body, kind="issue", state="open", created="2024-01-01T00:00:00Z"):
    return Chunk(f"issues:issue:{n}", f"issue:{n}", f"o/r#{n} {title}", body, f"https://github.com/o/r/issues/{n}",
                 {"kind": kind, "state": state, "number": n, "created": created, "author": "a"})


class LinksCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cfgdir = root / "cfg"
        (cfgdir / "projects").mkdir(parents=True); (cfgdir / "universes").mkdir()
        (cfgdir / "search.json").write_text(json.dumps({"data_dir": str(root / "data")}))
        (cfgdir / "projects" / "p.json").write_text(json.dumps({"id": "p", "title": "P", "sources": [
            {"id": "issues", "type": "github", "label": "issues", "color": "#112233", "priority": 2, "repo": "o/r", "include": ["issues", "prs"]}]}))
        (cfgdir / "universes" / "u.json").write_text(json.dumps({"id": "u", "title": "U", "projects": ["p"], "default": True}))
        (cfgdir / "universes" / "empty.json").write_text(json.dumps({"id": "empty", "title": "E", "projects": ["p"]}))
        self.cfg = config.load(cfgdir)
        st = Store(self.cfg.project_db("p"))
        st.apply("issues", [issue(1, "Crash", "x", created="2024-01-01T00:00:00Z"), issue(2, "Fix", "Fixes #1", kind="pr", state="merged", created="2024-01-05T00:00:00Z"),
                            issue(3, "Follow-up", "see #2 and #1", created="2024-01-09T00:00:00Z")])
        st.commit()
        links.compute(self.cfg, "u", force=True, run=None)
        self.cat = parse({"backends": {"s": {"adapter": "search", "python": "py", "index_dir": str(SEARCH), "config_dir": str(cfgdir), "est_mem_gb": 1}}}, Path("/base"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_neighbourhood_with_counts_and_get_refs(self):
        d = search_links(self.cat, "o/r#1")
        self.assertEqual((d["found"], d["node"]["id"], d["backend"]), (True, "o/r#1", "s"))
        self.assertEqual(d["counts"], {"closed_by": 1, "mentioned_by": 1})
        by = {x["rel"]: x for x in d["links"]}
        self.assertEqual((by["closed_by"]["id"], by["closed_by"]["conf"], by["closed_by"]["get_ref"]), ("o/r#2", 0.85, "p/issues:issue:2"))
        self.assertEqual(by["closed_by"]["how"], "body")
        self.assertTrue(by["closed_by"]["snip"])
        self.assertEqual([x["rel"] for x in search_links(self.cat, "#1", types=["mentioned_by"])["links"]], ["mentioned_by"])
        self.assertEqual(len(search_links(self.cat, "o/r#1", limit=1)["links"]), 1)

    def test_a_hit_ref_and_a_bare_number_name_the_document(self):
        self.assertEqual(search_links(self.cat, "p/issues:issue:2")["node"]["id"], "o/r#2")
        self.assertEqual(search_links(self.cat, "#3")["node"]["id"], "o/r#3")

    def test_story_in_time_order(self):
        d = search_links(self.cat, "o/r#1", story=True)
        self.assertEqual([x["id"] for x in d["story"]], ["o/r#1", "o/r#2", "o/r#3"])
        self.assertEqual(d["story"][1]["via"], {"from": "o/r#1", "rel": "closed_by"})

    def test_unknowns_are_answers_and_bad_input_is_an_api_error(self):
        self.assertEqual(search_links(self.cat, "o/r#999")["found"], False)
        self.assertEqual(search_links(self.cat, "nonsense")["found"], False)
        self.assertEqual(search_links(self.cat, "o/r#1", universe="empty").get("available"), False)    # no links database for that universe yet
        for kw, status in (({"types": ["closes_ish"]}, 400), ({"limit": 0}, 400), ({"universe": "nope"}, 404)):
            with self.assertRaises(ApiError) as cm:
                search_links(self.cat, "o/r#1", **kw)
            self.assertEqual(cm.exception.status, status, kw)


if __name__ == "__main__":
    unittest.main()
