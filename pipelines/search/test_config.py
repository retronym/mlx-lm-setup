#!/usr/bin/env python3
"""Config loader tests: the shipped config is valid, and every kind of mistake is reported (all at once, with file and key path)."""
import json, os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
import config

GIT = {"id": "code", "type": "git", "label": "code", "repo": "o/r", "ref": "main", "paths": ["src"], "chunkers": {".scala": "scala"}}
GH = {"id": "issues", "type": "github", "label": "issues", "repo": "o/r", "include": ["issues", "comments"]}
REL = {"id": "rel", "type": "github_releases", "label": "rel", "repo": "o/r"}


def write(root, search=None, projects=(), universes=()):
    (root / "projects").mkdir(parents=True, exist_ok=True)
    (root / "universes").mkdir(parents=True, exist_ok=True)
    (root / "search.json").write_text(json.dumps(search if search is not None else {}))
    for p in projects:
        (root / "projects" / f"{p['id']}.json").write_text(json.dumps(p))
    for u in universes:
        (root / "universes" / f"{u['id']}.json").write_text(json.dumps(u))


def proj(pid="p", *sources):
    return {"id": pid, "title": pid, "sources": list(sources) or [GIT]}


def uni(uid="u", *projects, **kw):
    return {"id": uid, "title": uid, "projects": list(projects) or ["p"], **kw}


class ConfigTests(unittest.TestCase):
    def load(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d), **kw)
            return config.load(d)

    def errors(self, **kw):
        with self.assertRaises(config.ConfigError) as cm:
            self.load(**kw)
        return cm.exception.errors

    def test_shipped_config_is_valid(self):
        cfg = config.load()
        u = cfg.default_universe()
        self.assertEqual(u.id, "scala-zinc")
        self.assertEqual(set(u.projects), {"scala2", "scala3", "scala-dev", "zinc", "scala-asm"})
        srcs = {s.key: s for s in cfg.universe_sources()}
        self.assertEqual(srcs["scala3/issues"].priority, 8)                     # the big trackers are the lowest priority
        self.assertIsNotNone(srcs["scala3/issues"].max_items_per_run)
        self.assertEqual(srcs["scala-asm/code"].ref, "main")
        self.assertTrue(all(any(s.type == "github_releases" for s in cfg.projects[p].sources) for p in ("scala2", "scala3", "zinc", "scala-asm")))
        self.assertTrue(all(any(s.type == "git_log" for s in cfg.projects[p].sources) for p in ("scala2", "scala3", "zinc", "scala-asm")))     # commit messages are indexed
        self.assertGreater(srcs["scala3/commits"].priority, srcs["zinc/commits"].priority)
        self.assertEqual(cfg.project_db("zinc").name, "index.db")

    def test_defaults_are_filled_in(self):
        cfg = self.load(projects=[proj("p", GIT, GH, REL)], universes=[uni()])
        git, gh, rel = cfg.projects["p"].sources
        self.assertEqual((git.priority, git.enabled, git.min_interval_hours, git.max_items_per_run), (5, True, 0, None))
        self.assertEqual(gh.since, "2000-01-01T00:00:00Z")                      # from search.json github.default_since
        self.assertFalse(rel.tag_messages)
        self.assertEqual(cfg.search["embedder"]["model"], "Qwen/Qwen3-Embedding-0.6B")
        self.assertTrue(cfg.default_universe().id == "u")                       # a single universe is the default even if not marked

    def test_every_problem_is_reported_with_file_and_path(self):
        bad = proj("p", {**GIT, "priority": 12, "paths": [], "chunkers": {"scala": "scala"}}, {**GH, "include": ["wat"], "extra": 1}, {**REL, "repo": "nope"})
        errs = self.errors(projects=[bad], universes=[uni("u", "p", "ghost")])
        text = "\n".join(errs)
        for needle in ("projects/p.json: sources[0].priority", "sources[0].paths: expected at least 1", "suffix keys start with a dot",
                       "sources[1].include: item 0: expected one of", "sources[1].extra: unknown key", "sources[2].repo: expected owner/name",
                       "universes/u.json: projects: unknown project 'ghost'"):
            self.assertIn(needle, text)
        self.assertGreaterEqual(len(errs), 7)                                   # all at once, not just the first

    def test_structural_errors(self):
        self.assertIn("duplicate source id", "\n".join(self.errors(projects=[proj("p", GIT, GIT)], universes=[uni()])))
        with tempfile.TemporaryDirectory() as d:                                 # a project whose id differs from its file name
            write(Path(d), projects=[proj("p")], universes=[uni()])
            (Path(d) / "projects" / "p.json").rename(Path(d) / "projects" / "renamed.json")
            with self.assertRaises(config.ConfigError) as cm:
                config.load(d)
            self.assertIn("must match the file name ('renamed')", "\n".join(cm.exception.errors))
        self.assertIn("comments need issues and/or prs", "\n".join(self.errors(projects=[proj("p", {**GH, "include": ["comments"]})], universes=[uni()])))
        self.assertIn("more than one universe is marked default", "\n".join(self.errors(projects=[proj("p")], universes=[uni("a", default=True), uni("b", default=True)])))
        self.assertIn("unknown key", "\n".join(self.errors(search={"surprise": 1}, projects=[proj("p")], universes=[uni()])))
        self.assertIn("HH:MM", "\n".join(self.errors(search={"refresh": {"at": "3am"}}, projects=[proj("p")], universes=[uni()])))
        self.assertIn("expected a #rrggbb colour", "\n".join(self.errors(projects=[proj("p", {**GIT, "color": "red"})], universes=[uni()])))

    def test_two_sources_may_not_index_the_same_kind_of_item(self):
        a = {**GH, "id": "a", "include": ["issues", "comments"]}
        b = {**GH, "id": "b", "include": ["prs", "comments"]}
        self.load(projects=[proj("p", a, b)], universes=[uni()])                                   # issues+their comments vs PRs+their comments: disjoint
        errs = "\n".join(self.errors(projects=[proj("p", a, {**GH, "id": "c", "include": ["issues", "reviews"]})], universes=[uni()]))
        self.assertIn("sources 'a' and 'c' both index issues of o/r", errs)
        errs = "\n".join(self.errors(projects=[proj("p", {**GH, "id": "a", "include": ["issues", "prs", "comments"]}, b)], universes=[uni()]))
        self.assertIn("both index comments on PRs of o/r", errs)
        self.load(projects=[proj("p", a, {**GH, "id": "other", "repo": "o/elsewhere"})], universes=[uni()])   # a different repo is no overlap

    def test_git_log_sources(self):
        log = {"id": "commits", "type": "git_log", "label": "commits", "repo": "o/r", "ref": "main"}
        s = self.load(projects=[proj("p", log)], universes=[uni()]).projects["p"].sources[0]
        self.assertEqual((s.merges, s.paths, s.since), (False, (), "2000-01-01T00:00:00Z"))
        self.assertIn("scala-steward", s.skip_authors)                                            # dependency bumps are skipped by default
        s = self.load(projects=[proj("p", {**log, "merges": True, "paths": ["src"], "since": "2018-01-01T00:00:00Z", "skip_authors": []})], universes=[uni()]).projects["p"].sources[0]
        self.assertEqual((s.merges, s.paths, s.since, s.skip_authors), (True, ("src",), "2018-01-01T00:00:00Z", ()))
        errs = "\n".join(self.errors(projects=[proj("p", {k: v for k, v in {**log, "merges": "yes", "include": ["issues"]}.items() if k != "ref"})], universes=[uni()]))
        self.assertIn("ref: required", errs); self.assertIn("merges: expected true or false", errs); self.assertIn("include: unknown key", errs)

    def test_invalid_json_and_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write(root, projects=[proj("p")], universes=[uni()])
            (root / "projects" / "broken.json").write_text("{not json")
            with self.assertRaises(config.ConfigError) as cm:
                config.load(root)
            self.assertIn("broken.json: invalid JSON", "\n".join(cm.exception.errors))
            (root / "search.json").unlink()
            with self.assertRaises(config.ConfigError) as cm:
                config.load(root)
            self.assertIn("search.json: file not found", "\n".join(cm.exception.errors))

    def test_universes_compose_projects_independently(self):
        cfg = self.load(projects=[proj("a"), proj("b", {**GIT, "id": "x"})], universes=[uni("both", "a", "b", default=True), uni("only-b", "b")])
        self.assertEqual([s.key for s in cfg.universe_sources("both")], ["a/code", "b/x"])
        self.assertEqual([s.key for s in cfg.universe_sources("only-b")], ["b/x"])      # one project, several universes
        with self.assertRaises(KeyError):
            cfg.universe("nope")


if __name__ == "__main__":
    unittest.main()
