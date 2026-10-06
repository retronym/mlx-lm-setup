#!/usr/bin/env python3
"""The Discourse source without the network: a fake forum answers the same JSON paths the real one does (shapes taken from the pilot crawl)."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
sys.path.insert(0, os.path.dirname(__file__))
import config
from store import Store
from sources import discourse
from sources.discourse import Discourse, Gone, clean, urls

SITE = "forum.example.org"


def source(cap=None, since="2000-01-01T00:00:00Z"):
    return config.Source(project="p", id="forum", type="discourse", label="forum", color="#000000", priority=5, enabled=True, min_interval_hours=0,
                         max_items_per_run=cap, repo=SITE, since=since)


def post(pid, n, raw, user="alice", **kw):
    return {"id": pid, "post_number": n, "raw": raw, "cooked": kw.pop("cooked", f"<p>{raw}</p>"), "username": user, "name": user.title(), "post_type": 1,
            "action_code": None, "hidden": False, "deleted_at": None, "user_deleted": False, "created_at": f"2024-01-{n:02d}T10:00:00.000Z",
            "updated_at": f"2024-01-{n:02d}T10:00:00.000Z", "reply_to_post_number": None, "actions_summary": [{"id": 2, "count": n}], **kw}


class Forum:
    """Topics {id: {"title", "bumped", "posts": [post]}}; serves /latest.json, /t/{id}.json (first 20 posts), /t/{id}/posts.json, counting requests."""

    def __init__(self, topics, page_size=2):
        self.topics, self.page_size, self.paths = topics, page_size, []
        self.requests, self.waited = 0, 0.0

    def get(self, path):
        self.paths.append(path)
        self.requests += 1
        u = urlparse(path)
        q = parse_qs(u.query)
        if u.path == "/about.json":
            return {"about": {"stats": {"topics_count": len(self.topics)}}}
        if u.path == "/categories.json":
            return {"category_list": {"categories": [{"id": 9, "slug": "language-design", "subcategory_list": []}]}}
        if u.path == "/latest.json":
            page = int(q.get("page", ["0"])[0])
            order = sorted(self.topics.items(), key=lambda kv: kv[1]["bumped"], reverse=True)
            tl = [{"id": tid, "bumped_at": t["bumped"], "pinned": False, "posts_count": len(t["posts"])} for tid, t in order]
            chunk = tl[page * self.page_size:(page + 1) * self.page_size]
            more = (page + 1) * self.page_size < len(tl)
            return {"topic_list": {"topics": chunk, "more_topics_url": f"/latest?page={page + 1}" if more else None}}
        parts = u.path.strip("/").split("/")
        tid = int(parts[1].removesuffix(".json"))
        if tid not in self.topics:
            raise Gone(path)
        t = self.topics[tid]
        if len(parts) == 2:
            return {"id": tid, "title": t["title"], "slug": f"topic-{tid}", "category_id": 9, "posts_count": len(t["posts"]), "views": 5, "closed": False,
                    "post_stream": {"posts": [dict(p) for p in t["posts"][:20]], "stream": [p["id"] for p in t["posts"]]}}
        ids = [int(i) for i in q["post_ids[]"]]
        assert len(ids) <= 100
        return {"post_stream": {"posts": [dict(p) for p in t["posts"] if p["id"] in ids]}}


def forum():
    long = [post(1000 + i, i, f"Reply number {i} about givens." if i > 1 else "Opening: should givens be named?", user="bob" if i % 2 else "carol") for i in range(1, 46)]
    return Forum({
        1: {"title": "Pre-SIP: named givens", "bumped": "2024-03-01T00:00:00.000Z", "posts": long},
        2: {"title": "Core team notes", "bumped": "2024-02-01T00:00:00.000Z",
            "posts": [post(2001, 1, "Notes. See https://github.com/scala/scala3/pull/26497 and [the thread](https://forum.example.org/t/named-givens/1/3).",
                           cooked='<p>Notes. <a href="https://github.com/scala/scala3/pull/26497">pr</a> <a href="/t/named-givens/1/3">thread</a> <a class="mention" href="/u/bob">@bob</a></p>'),
                      post(2002, 2, "", post_type=3, action_code="closed.enabled")]},
        3: {"title": "Old idea", "bumped": "2023-01-01T00:00:00.000Z", "posts": [post(3001, 1, "An old idea.")]},
    })


class CleanTests(unittest.TestCase):
    def test_quotes_are_cut_to_who_and_their_first_words(self):
        raw = '[quote="lihaoyi, post:10, topic:4702"]\n' + "Historically we can look at F# and Haskell. " * 10 + "\n[/quote]\n\nI would have preferred F#."
        t = clean(raw)
        self.assertTrue(t.startswith("> @lihaoyi (post 10): Historically"), t)
        self.assertLess(len(t), 260)
        self.assertTrue(t.endswith("I would have preferred F#."))

    def test_nested_quotes_and_plain_quotes(self):
        t = clean('[quote="a, post:1, topic:2"]outer [quote="b, post:2, topic:2"]inner[/quote] more[/quote]\nreply\n[quote]anonymous[/quote]')
        self.assertIn("> @a (post 1): outer > @b (post 2): inner more", t)
        self.assertIn("> anonymous", t)
        self.assertNotIn("[quote", t)

    def test_images_and_details(self):
        self.assertEqual(clean("![diagram|690x388](upload://abc.png) and ![image](upload://x.png)"), "[image: diagram] and")
        self.assertEqual(clean('[details="Long log"]\nstack\n[/details]'), "Long log:\nstack")

    def test_urls_from_cooked_html(self):
        got = urls('<a href="/t/x/12/3">t</a> <a class="mention" href="/u/bob">@bob</a> <a href="https://github.com/o/r/pull/1?a=1&amp;b=2">p</a> '
                   '<a href="//forum.example.org/uploads/default/x.png">img</a> <a href="#heading">h</a> <a href="https://github.com/o/r/pull/1?a=1&b=2">again</a>', SITE)
        self.assertEqual(got, ["https://forum.example.org/t/x/12/3", "https://github.com/o/r/pull/1?a=1&b=2"])


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, f, **kw):
        Discourse(source(**{k: v for k, v in kw.items() if k in ("cap", "since")}), client=f).sync(self.st, log=lambda *_: None)

    def rows(self):
        return {r[0]: (r[1], r[2], json.loads(r[3])) for r in self.st.db.execute("SELECT id, doc, title, meta FROM chunks")}

    def test_first_run_reads_every_topic_and_post(self):
        f = forum()
        self.sync(f)
        rows = self.rows()
        self.assertEqual(len([r for r in rows.values() if r[0] == "topic:1"]), 45)                 # 20 with the topic, 25 more in one posts.json request
        self.assertEqual(sum(1 for p in f.paths if p.startswith("/t/1/posts.json")), 1)
        head = rows["forum:post:1001"]
        self.assertEqual((head[1], head[2]["kind"], head[2]["author"], head[2]["author_name"], head[2]["labels"]), ("Pre-SIP: named givens", "topic", "bob", "Bob", ["language-design"]))
        reply = rows["forum:post:1002"]
        self.assertEqual((reply[1], reply[2]["kind"], reply[2]["topic"], reply[2]["post_number"], reply[2]["likes"]), ("Pre-SIP: named givens  (reply #2 by carol)", "post", 1, 2, 2))
        self.assertNotIn("forum:post:2002", rows)                                                   # "closed this topic" is no post
        self.assertEqual(rows["forum:post:2001"][2]["urls"], ["https://github.com/scala/scala3/pull/26497", "https://forum.example.org/t/named-givens/1/3"])
        self.assertEqual(self.st.get("forum", "fwd"), "2024-03-01T00:00:00.000Z")
        self.assertEqual(self.st.get("forum", "back"), "2023-01-01T00:00:00.000Z")
        self.assertEqual(self.st.get("forum", "bf_done"), "1")

    def test_a_new_reply_fetches_the_topic_and_only_the_new_post(self):
        f = forum()
        self.sync(f)
        f.topics[1]["posts"].append(post(1046, 46, "A late reply.", user="dave"))
        f.topics[1]["bumped"] = "2024-04-01T00:00:00.000Z"
        f.paths.clear()
        self.sync(f)
        self.assertEqual([p.split("?")[0] for p in f.paths if p.startswith("/t/")], ["/t/1.json", "/t/1/posts.json"])
        self.assertIn("post_ids[]=1046", [p for p in f.paths if "posts.json" in p][0])
        self.assertEqual([p for p in f.paths if p.startswith("/t/1/posts")][0].count("post_ids"), 1)
        self.assertIn("forum:post:1046", self.rows())
        self.assertEqual(self.st.get("forum", "fwd"), "2024-04-01T00:00:00.000Z")

    def test_nothing_new_costs_one_listing_page(self):
        f = forum()
        self.sync(f)
        f.paths.clear()
        self.sync(f)
        self.assertEqual([p.split("?")[0] for p in f.paths], ["/latest.json"])

    def test_the_cap_spreads_the_backfill_over_runs_newest_first(self):
        f = forum()
        self.sync(f, cap=1)
        self.assertEqual({r[0] for r in self.rows().values()}, {"topic:1"})
        self.assertNotEqual(self.st.get("forum", "bf_done"), "1")
        self.sync(f, cap=1)
        self.assertEqual({r[0] for r in self.rows().values()}, {"topic:1", "topic:2"})
        self.sync(f, cap=1)
        self.assertEqual({r[0] for r in self.rows().values()}, {"topic:1", "topic:2", "topic:3"})
        self.sync(f, cap=1)
        self.assertEqual(self.st.get("forum", "bf_done"), "1")

    def test_the_horizon_stops_the_backfill(self):
        f = forum()
        self.sync(f, since="2024-01-01T00:00:00Z")
        self.assertEqual({r[0] for r in self.rows().values()}, {"topic:1", "topic:2"})
        self.assertEqual(self.st.get("forum", "bf_done"), "1")

    def test_deleted_posts_and_topics_disappear(self):
        f = forum()
        self.sync(f)
        f.topics[1]["posts"] = [p for p in f.topics[1]["posts"] if p["id"] != 1030]
        f.topics[1]["bumped"] = "2024-05-01T00:00:00.000Z"
        del f.topics[3]
        self.sync(f)
        self.assertNotIn("forum:post:1030", self.rows())
        self.assertIn("forum:post:3001", self.rows())                 # a sync does not look at old topics ...
        Discourse(source(), client=f).reconcile(self.st, log=lambda *_: None)
        self.assertNotIn("forum:post:3001", self.rows())              # ... the reconcile does

    def test_a_renamed_topic_rereads_every_post(self):
        f = forum()
        self.sync(f)
        f.topics[1]["title"] = "SIP-99: named givens"
        f.topics[1]["bumped"] = "2024-05-01T00:00:00.000Z"
        self.sync(f)
        titles = {r[1] for r in self.rows().values() if r[0] == "topic:1"}
        self.assertTrue(all(t.startswith("SIP-99") for t in titles), titles)

    def test_long_posts_are_split(self):
        f = Forum({7: {"title": "Long", "bumped": "2024-01-01T00:00:00.000Z", "posts": [post(7001, 1, "\n\n".join(["A paragraph about variance. " * 20] * 12))]}})
        self.sync(f)
        ids = sorted(self.rows())
        self.assertEqual(ids[0], "forum:post:7001")
        self.assertGreater(len(ids), 1)
        self.assertTrue(all(i.startswith("forum:post:7001~") for i in ids[1:]))


class ClientTests(unittest.TestCase):
    def test_rate_limit_waits_and_slows_down(self):
        import io, urllib.error
        calls = []

        def opener(req, timeout):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "3"}, None)
            return io.BytesIO(b'{"ok": true}')
        c = discourse.Client(SITE, log=lambda *_: None, opener=opener)
        slept = []
        c._pause = lambda s: slept.append(s)
        self.assertEqual(c.get("/x.json"), {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertIn(3.0, slept)
        self.assertGreater(c.delay, discourse.POLITE["delay_s"])
        self.assertEqual(calls[0], f"https://{SITE}/x.json")

    def test_gone(self):
        import urllib.error

        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        c = discourse.Client(SITE, log=lambda *_: None, opener=opener)
        c._pause = lambda s: None
        with self.assertRaises(Gone):
            c.get("/t/1.json")


if __name__ == "__main__":
    unittest.main()
