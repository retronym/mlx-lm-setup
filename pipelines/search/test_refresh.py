#!/usr/bin/env python3
"""The refresh script, its phases and the local-model jobs, against a fake gateway (embeddings, chat, NLI, search) and scratch config, data and
git repos (run with the .venv-jev python). Covers: the lock, the phase plan and tiers, the time budget, embedding through the gateway, the
digest and thread summaries including the faithfulness retry, verify and its canaries, and one whole refresh end to end."""
import hashlib, json, os, subprocess, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np
import config, digest, embed, enrich, llm, refresh, runstate, sync, verify
import gateway_client
from store import Store, Chunk
from sources import ghissues
from test_github import FakeGitHub, issue, comment, NOW


class FakeGateway:
    """Just enough of the gateway: /v1/embeddings, /v1/chat/completions, /api/entail, /api/search."""

    def __init__(self):
        self.chat_calls, self.embed_calls, self.unfaithful_first, self.partly_unfaithful = [], 0, 0, False
        gw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                out = getattr(gw, "h" + self.path.replace("/", "_").replace("-", "_"))(body)
                raw = json.dumps(out).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers()
                self.wfile.write(raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()

    @staticmethod
    def vec(text):
        h = hashlib.sha256(text.encode()).digest()
        v = np.frombuffer(h[:8], dtype=np.uint8).astype(np.float32) + 1
        return (v / np.linalg.norm(v)).tolist()

    def h_v1_embeddings(self, b):
        self.embed_calls += 1
        texts = [b["input"]] if isinstance(b["input"], str) else b["input"]
        return {"object": "list", "model": "fake-embed", "data": [{"index": i, "embedding": self.vec(t)} for i, t in enumerate(texts)]}

    def h_v1_chat_completions(self, b):
        prompt = " ".join(m["content"] for m in b["messages"])
        self.chat_calls.append(prompt)
        if self.unfaithful_first > 0:
            self.unfaithful_first -= 1
            text = "The maintainers decided to rewrite the whole compiler in Rust, which is UNFAITHFUL to the source text."
        elif self.partly_unfaithful and "<facts>" in prompt:
            text = "## Proj\n- PR sbt/zinc#1500 was merged after a long discussion between the maintainers.\n- The maintainers decided to rewrite everything in Rust, which is UNFAITHFUL to the facts.\n"
        elif "<facts>" in prompt:
            text = "Zinc saw activity this week. PR sbt/zinc#1500 was merged after discussion. Release v1.11.0 was published."
        else:
            text = "The thread reports a crash in the typer. The reporter and the maintainers discussed it at length. It was fixed in a later release."
        return {"choices": [{"message": {"content": text}}]}

    def h_api_entail(self, b):
        return {"probs": [[0.9, 0.05, 0.05] if "UNFAITHFUL" in h else [0.01, 0.9, 0.09] for h in b["hypotheses"]]}

    def h_api_search(self, b):
        needle = "needle" in b["query"].lower()
        return {"results": [{"title": "Needle.scala  p  Needle.find" if needle else "Other.scala", "url": f"https://github.com/o/r/blob/x/{'Needle' if needle else 'Other'}.scala"}]}


def write_config(root, canaries=None, llm=None):
    (root / "projects").mkdir(parents=True); (root / "universes").mkdir()
    (root / "search.json").write_text(json.dumps({"data_dir": str(root / "data"), "embedder": {"model": "fake-embed", "via": "gateway", "batch": 4},
                                                  "llm": llm or {"thread_summaries": {"enabled": True, "min_comments": 3, "max_per_run": 2}, "digest": {"enabled": True}}}))
    (root / "projects" / "p.json").write_text(json.dumps({"id": "p", "title": "Proj", "sources": [
        {"id": "code", "type": "git", "label": "code", "repo": "o/r", "ref": "main", "paths": ["."], "chunkers": {".scala": "scala"}, "priority": 2},
        {"id": "issues", "type": "github", "label": "issues", "repo": "o/gh", "include": ["issues", "comments"], "since": "2026-01-01T00:00:00Z", "priority": 3}]}))
    (root / "universes" / "u.json").write_text(json.dumps({"id": "u", "title": "U", "projects": ["p"]}))
    (root / "canaries.json").write_text(json.dumps(canaries or {"queries": [{"q": "where is the needle", "expect": ["Needle"]}]}))


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_config(self.root / "cfg")
        os.environ["SEARCH_CONFIG_DIR"] = str(self.root / "cfg")
        self.cfg = config.load()
        self.gw = FakeGateway()
        p = mock.patch.object(gateway_client, "BASE", self.gw.url)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.gw.close)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("SEARCH_CONFIG_DIR", None))

    def store(self):
        return Store(self.cfg.project_db("p"))

    def thread(self, st, n, comments, issue_updated="2026-02-01T00:00:00Z"):
        chunks = [Chunk(f"issues:issue:{n}", f"issue:{n}", f"o/gh#{n} title {n}", f"body {n}", f"https://github.com/o/gh/issues/{n}",
                        {"kind": "issue", "state": "open", "number": n, "updated": issue_updated})]
        base = ghissues._ts(issue_updated)                                          # a thread's activity runs on from its issue
        chunks += [Chunk(f"issues:comment:{n}{i}", f"issue:{n}", f"o/gh#{n} title {n}  (comment by u{i})", f"comment text {i} about the typer", f"https://github.com/o/gh/issues/{n}#c{i}",
                         {"kind": "comment", "number": n, "updated": ghissues._iso(base + (i + 1) * 3600)}) for i in range(comments)]
        st.apply("issues", chunks); st.commit()


class RunState(Case):
    def test_lock_is_exclusive_and_released(self):
        with runstate.lock(self.cfg):
            with self.assertRaises(runstate.Busy) as cm:
                with runstate.lock(self.cfg):
                    pass
            self.assertIn("another indexer run is active", str(cm.exception))
        with runstate.lock(self.cfg):                                              # free again
            pass

    def test_phases_are_tracked(self):
        run = runstate.Run(self.cfg, "refresh")
        run.phases(["sync", "embed"]); run.phase("sync"); run.phase("embed"); run.finish()
        s = runstate.read(self.cfg)
        self.assertEqual((s["phases"], s["phases_done"], s["ok"], s["running"]), (["sync", "embed"], ["sync", "embed"], True, False))

    def test_a_dead_unfinished_run_is_stale(self):
        run = runstate.Run(self.cfg, "sync")
        run.s["pid"] = 2 ** 22 + 12345; run._write(force=True)                    # no such process
        s = runstate.read(self.cfg)
        self.assertEqual((s["running"], s["stale"]), (False, True))


class Plan(Case):
    def steps(self, now=None, state=None, **kw):
        args = {"only": set(), "skip": set(), **kw}
        return {ph: (on, why) for ph, on, why in refresh.plan(self.cfg, "u", [], args, state or {}, now or time.time())}

    def test_defaults(self):
        s = self.steps()
        self.assertTrue(all(s[p][0] for p in ("sync", "reconcile", "enrich", "digest", "embed", "verify")))     # reconcile is due on a fresh install
        s = self.steps(state={"last_reconcile": time.time() - 3600})
        self.assertFalse(s["reconcile"][0])
        self.assertIn("every 7", s["reconcile"][1])
        s = self.steps(state={"last_reconcile": time.time() - 8 * 86400})
        self.assertTrue(s["reconcile"][0])

    def test_only_and_skip(self):
        self.assertEqual([p for p, (on, _) in self.steps(only={"embed", "verify"}).items() if on], ["embed", "verify"])
        self.assertFalse(self.steps(skip={"digest"})["digest"][0])

    def test_tiers_filter_by_priority(self):
        real = config.load(config.CONFIG_DIR)
        high = embed.targets(real, [], real.search["refresh"]["tiers"]["high"]["max_priority"])
        everything = embed.targets(real, [], 9)
        self.assertTrue(0 < len(high) < len(everything))
        self.assertTrue(all(s.priority <= 3 for s in high))
        self.assertEqual([s.priority for s in everything], sorted(s.priority for s in everything))                 # priority order


class EmbedViaGateway(Case):
    def test_vectors_come_from_the_gateway_and_are_stored_under_its_model_name(self):
        st = self.store()
        st.apply("code", [Chunk(f"code:{i}", f"f{i}.scala", f"f{i}", f"def f{i} = {i} // padding padding", "u") for i in range(6)]); st.commit()
        emb = embed.load(self.cfg)                                                  # via = gateway in this config
        self.assertEqual((emb.name, emb.dev), ("fake-embed", "gateway"))
        run = runstate.Run(self.cfg, "embed")
        n = embed.run_embed(self.cfg, [self.cfg.projects["p"].source("code")], run, emb)
        self.assertEqual(n, 6)
        self.assertEqual(st.db.execute("SELECT count(*), count(DISTINCT model) FROM vec").fetchone(), (6, 1))
        self.assertEqual(st.db.execute("SELECT model FROM vec LIMIT 1").fetchone()[0], "fake-embed")
        self.assertGreaterEqual(self.gw.embed_calls, 2)                             # a probe, then batches
        self.assertEqual(embed.run_embed(self.cfg, [self.cfg.projects["p"].source("code")], run, emb), 0)     # nothing left to embed
        q = emb.query("hello")
        self.assertEqual(q.shape, (8,))

    def test_time_budget_stops_before_the_next_source(self):
        st = self.store(); st.apply("code", [Chunk("code:1", "f", "t", "text text text text text", "u")]); st.commit()
        run = runstate.Run(self.cfg, "embed")
        n = embed.run_embed(self.cfg, [self.cfg.projects["p"].source("code")], run, embed.load(self.cfg), deadline=time.time() - 1)
        self.assertEqual(n, 0)
        self.assertEqual(st.db.execute("SELECT count(*) FROM vec").fetchone()[0], 0)
        self.assertEqual(sync.run_sync(self.cfg, self.cfg.projects["p"].sources, run, deadline=time.time() - 1), 0)      # nothing started, nothing failed


class Digest(Case):
    def test_digest_from_facts_with_a_faithfulness_retry(self):
        st = self.store()
        self.thread(st, 1500, 4, issue_updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        since = time.time() - 3600
        fx = digest.facts(self.cfg, "u", since)
        self.assertEqual(fx[0][0], "Proj")
        self.assertTrue(any("o/gh#1500" in l and "open" in l for l in fx[0][1]))
        st.apply("issues", [Chunk("issues:issue:1500~1", "issue:1500", "o/gh#1500 title 1500", "a long body, second part", "u", {"kind": "issue", "state": "open", "number": 1500, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})])
        st.commit()
        again = digest.facts(self.cfg, "u", since)[0][1]
        self.assertEqual(sum(1 for l in again if "o/gh#1500" in l), 1)                              # a body split into two chunks is still one item
        self.assertTrue(any("4 new comments" in l for l in again))
        self.gw.unfaithful_first = 1                                                # the first answer invents something; the NLI gate sends it back
        d = digest.make_digest(self.cfg, "u", since)
        self.assertTrue(d["checked"] and d["overview"]); self.assertEqual(d["attempts"], 2)
        self.assertIn("o/gh#1500", d["text"].split("## Proj")[1])                    # the facts follow the overview as exact bullets
        self.assertIn("Fix these problems", self.gw.chat_calls[-1])
        self.assertEqual(json.loads(self.cfg.data_path("digest.json").read_text())["text"], d["text"])
        self.assertTrue(list(self.cfg.data_path("digests", "u").glob("*.md")))

    def test_commits_appear_in_the_facts(self):
        st = self.store()
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        st.apply("code", [Chunk(f"code:commit:{i:040x}", f"commit:{i}", f"o/r commit {i:08x} Fix the typer {i}", f"Fix the typer {i}\n\nFiles changed: a.scala", "u",
                                {"kind": "commit", "updated": recent}) for i in range(5)]
                 + [Chunk("code:commit:old", "commit:old", "o/r commit 00000000 An old one", "old", "u", {"kind": "commit", "updated": "2020-01-01T00:00:00Z"})])
        st.commit()
        lines = digest.facts(self.cfg, "u", time.time() - 3600)[0][1]
        line = next(l for l in lines if "commits landed" in l)
        self.assertIn("5 commits landed", line); self.assertIn("Fix the typer", line); self.assertNotIn("An old one", line)

    def test_unsupported_lines_are_pruned_after_the_retries(self):
        self.thread(self.store(), 7, 1, issue_updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.gw.partly_unfaithful = True
        d = digest.make_digest(self.cfg, "u", time.time() - 3600)
        self.assertEqual((d["checked"], d["overview"], d["attempts"], d["pruned"]), (True, True, 3, 1))     # the bad line went, the good one stayed
        self.assertNotIn("Rust", d["text"]); self.assertIn("merged after a long discussion", d["text"])

    def test_an_overview_that_cannot_be_made_faithful_is_left_out_but_the_facts_remain(self):
        st = self.store(); self.thread(st, 7, 1, issue_updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.gw.unfaithful_first = 99
        d = digest.make_digest(self.cfg, "u", time.time() - 3600)
        self.assertEqual((d["overview"], d["attempts"]), (False, 3))
        self.assertNotIn("Rust", d["text"]); self.assertIn("o/gh#7", d["text"])                    # nothing invented reaches the page; the exact facts do

    def test_nothing_changed_means_no_model_call(self):
        self.thread(self.store(), 1, 1, issue_updated="2020-01-01T00:00:00Z")
        self.assertIsNone(digest.make_digest(self.cfg, "u", time.time() - 3600))
        self.assertEqual(self.gw.chat_calls, [])


class ThreadSummaries(Case):
    def run_enrich(self, **kw):
        run = runstate.Run(self.cfg, "refresh")
        return enrich.summarize_threads(self.cfg, [self.cfg.projects["p"].source("issues")], run, **kw)

    def test_long_threads_get_a_checked_summary_and_only_when_they_grow(self):
        st = self.store()
        self.thread(st, 1, 2)                                                       # too short
        self.thread(st, 2, 4)
        self.assertEqual(self.run_enrich()[:2], (1, 0))
        row = st.db.execute("SELECT doc, json_extract(meta, '$.kind'), json_extract(meta, '$.comments'), title FROM chunks WHERE id = 'issues:summary:2'").fetchone()
        self.assertEqual(row[:3], ("issue:2", "summary", 4)); self.assertIn("thread summary", row[3])
        self.assertIn("[comment by u0]", self.gw.chat_calls[0])                     # the model saw the thread with its authors
        self.assertEqual(self.run_enrich()[:2], (0, 0))                             # unchanged: not summarised again
        self.thread(st, 2, 10)                                                      # grew past 1.5x and +5
        self.assertEqual(self.run_enrich()[:2], (1, 0))
        self.assertEqual(st.db.execute("SELECT json_extract(meta, '$.comments') FROM chunks WHERE id = 'issues:summary:2'").fetchone()[0], 10)

    def test_cap_and_newest_first(self):
        st = self.store()
        for n, when in ((10, "2026-01-01T00:00:00Z"), (11, "2026-03-01T00:00:00Z"), (12, "2026-02-01T00:00:00Z")):
            self.thread(st, n, 4, issue_updated=when)
        w, f, _ = self.run_enrich()                                                 # max_per_run is 2
        self.assertEqual(w, 2)
        have = {r[0] for r in st.db.execute("SELECT id FROM chunks WHERE id LIKE 'issues:summary:%'")}
        self.assertEqual(have, {"issues:summary:11", "issues:summary:12"})          # the two most recently active threads

    def test_unfaithful_summaries_are_not_indexed(self):
        st = self.store(); self.thread(st, 3, 4)
        self.gw.unfaithful_first = 99
        self.assertEqual(self.run_enrich()[:2], (0, 1))
        self.assertEqual(st.db.execute("SELECT count(*) FROM chunks WHERE id LIKE '%summary%'").fetchone()[0], 0)

    def test_disabled_by_default_in_the_shipped_config(self):
        real = config.load(config.CONFIG_DIR)
        self.assertFalse(real.search["llm"]["thread_summaries"]["enabled"])
        self.assertTrue(real.search["llm"]["digest"]["enabled"])


class Verify(Case):
    def test_integrity_and_pending_vectors(self):
        st = self.store(); st.apply("code", [Chunk("code:1", "f", "t", "some text here for the chunk", "u")]); st.commit()
        problems, counts = verify.integrity(self.cfg, "u")
        self.assertEqual((problems, counts["p"]), ([], {"chunks": 1, "pending_vectors": 1}))
        st.db.execute("DELETE FROM fts"); st.commit()
        self.assertIn("keyword index does not match", verify.integrity(self.cfg, "u")[0][0])

    def test_a_canary_can_ask_for_more_results(self):
        seen = []
        orig = gateway_client.post
        with mock.patch.object(gateway_client, "post", lambda path, body, **kw: (seen.append(body["k"]), orig(path, body, **kw))[1]):
            q = self.root / "k.json"
            q.write_text(json.dumps({"queries": [{"q": "where is the needle", "expect": ["Needle"], "k": 10}, {"q": "where is the needle", "expect": ["Needle"]}]}))
            self.assertEqual(verify.canaries(self.cfg, "u", q)[0], 2)
        self.assertEqual(seen, [10, 5])

    def test_canaries_pass_fail_and_skip(self):
        self.assertEqual(verify.canaries(self.cfg, "u")[0], 1)
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"queries": [{"q": "something unrelated", "expect": ["Needle"]}]}))
        passed, failed, skipped = verify.canaries(self.cfg, "u", bad)
        self.assertEqual((passed, len(failed), skipped), (0, 1, None))
        with mock.patch.object(gateway_client, "BASE", "http://127.0.0.1:9"):
            self.assertIn("unavailable", verify.canaries(self.cfg, "u")[2])           # no gateway: skipped, not failed


class WholeRefresh(Case):
    def test_end_to_end(self):
        repo = self.root / "r.git"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / "Needle.scala").write_text("package p\nobject Needle {\n  def find(x: Int): Int = x + 1 // find the needle\n}\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True); subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True, env=env)
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        gh = FakeGitHub([issue(5, recent)], [comment(50, 5, recent)])
        real_ensure = lambda cfg, r, fetch=True, log=print: repo / ".git"
        with mock.patch("repos.ensure", real_ensure), mock.patch.object(ghissues, "pages", gh.pages), mock.patch.object(ghissues, "PAGE_DELAY", 0), \
                mock.patch("sources.ghissues.time.time", return_value=time.time()):
            rc = refresh.main(["--skip", "reconcile"])
        self.assertEqual(rc, 0, runstate.read(self.cfg)["errors"])
        st = self.store()
        self.assertGreaterEqual(st.db.execute("SELECT count(*) FROM chunks WHERE source = 'code'").fetchone()[0], 1)
        self.assertEqual(st.db.execute("SELECT count(*) FROM chunks WHERE source = 'issues'").fetchone()[0], 2)
        self.assertEqual(st.db.execute("SELECT count(*) FROM chunks c LEFT JOIN vec v ON v.rowid = c.rowid AND v.hash = c.hash WHERE v.rowid IS NULL").fetchone()[0], 0)
        s = refresh.load_state(self.cfg)
        self.assertTrue(s["last_run"]["ok"], s["last_run"])
        self.assertEqual(list(s["last_run"]["phases"]), ["sync", "enrich", "digest", "embed", "neighbours", "links", "verify"])
        self.assertEqual(s["last_run"]["phases"]["verify"]["canaries_passed"], 1)
        self.assertEqual(s["history"][0]["ok"], True)
        self.assertTrue(self.cfg.data_path("digest.json").exists())                 # the recent issue made it into a digest
        rs = runstate.read(self.cfg)
        self.assertEqual((rs["kind"], rs["ok"], rs["running"], rs["phases_done"]), ("refresh", True, False, ["sync", "enrich", "digest", "embed", "neighbours", "links", "verify"]))

    def test_a_second_run_while_one_is_active_exits_3(self):
        with runstate.lock(self.cfg):
            self.assertEqual(refresh.main(["--only", "verify"]), 3)


if __name__ == "__main__":
    unittest.main()
