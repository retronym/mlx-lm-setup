#!/usr/bin/env python3
"""Search configuration: global settings, projects and universes, all JSON under pipelines/search/config/.

  config/search.json            global: data dirs, models, chunking, GitHub politeness, refresh tiers, local-model steps
  config/projects/<id>.json     a project: a title and its sources (git paths, GitHub issues/PRs/reviews, release notes)
  config/universes/<id>.json    a universe: a named list of projects searched together

A project is indexed on its own (its own database) and knows nothing about universes, so a project can sit in several of them.
Every file is validated, and all problems are reported together, each with its file and key path.

  python config.py check            validate everything, print the tree
  python config.py show <universe>  what a universe contains and what the refresh will do for it
"""
import json, os, re, sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).parent
CONFIG_DIR = HERE / "config"
CHUNKERS = ("scala", "java", "markdown", "plain", "scala_ts", "java_ts")
SOURCE_TYPES = ("git", "git_log", "github", "github_releases", "discourse")
DEFAULT_SKIP_AUTHORS = ["scala-steward", "dependabot[bot]", "github-actions[bot]", "renovate[bot]"]
GITHUB_INCLUDE = ("issues", "prs", "comments", "reviews")
# What a search hit can be, finer than its source: a chunk of a file in a git tree (code, docs, spec), an issue or PR body, a comment, a review
# comment on a diff, an LLM summary of a long thread, a commit message, release notes, an annotated tag's message, a forum topic's opening post, a reply in it.
KINDS = ("file", "issue", "pr", "comment", "review", "summary", "commit", "release", "tag", "topic", "post")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SITE_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ConfigError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("\n".join(errors))


# ---- a small declarative validator: field -> (kind, default); a default of REQUIRED means the key must be present ----------------
REQUIRED = object()


class V:
    def __init__(self, where, errors):
        self.where, self.errors = where, errors

    def err(self, path, msg):
        self.errors.append(f"{self.where}: {path}: {msg}")

    def obj(self, d, path, fields, allow_extra=("description",)):
        """Validate dict `d` against {key: (check, default)}; returns the dict with defaults filled in."""
        if not isinstance(d, dict):
            self.err(path, f"expected an object, got {type(d).__name__}")
            return {k: (None if dflt is REQUIRED else dflt) for k, (_, dflt) in fields.items()}
        for k in d:
            if k not in fields and k not in allow_extra:
                self.err(f"{path}.{k}" if path else k, f"unknown key (allowed: {', '.join(sorted(fields))})")
        out = {}
        for k, (check, dflt) in fields.items():
            if k not in d:
                if dflt is REQUIRED:
                    self.err(f"{path}.{k}" if path else k, "required")
                out[k] = None if dflt is REQUIRED else dflt
                continue
            msg = check(d[k])
            if msg:
                self.err(f"{path}.{k}" if path else k, msg)
            out[k] = d[k]
        return out


def t_str(v):
    return None if isinstance(v, str) and v else "expected a non-empty string"


def t_bool(v):
    return None if isinstance(v, bool) else "expected true or false"


def t_int(lo=None, hi=None, nullable=False):
    def check(v):
        if v is None and nullable:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            return "expected an integer"
        if lo is not None and v < lo or hi is not None and v > hi:
            return f"expected an integer in {lo}..{hi}"
    return check


def t_num(lo=None, nullable=False):
    def check(v):
        if v is None and nullable:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "expected a number"
        if lo is not None and v < lo:
            return f"expected a number >= {lo}"
    return check


def t_blend(v):
    """[[up to fused rank, weight of the retrieval score 0..1], ...], ranks ascending, or null for the reranker alone."""
    if v is None:
        return None
    if not isinstance(v, list) or not v or not all(isinstance(r, list) and len(r) == 2 and isinstance(r[0], int) and isinstance(r[1], (int, float))
                                                   and not isinstance(r[1], bool) and 0 <= r[1] <= 1 for r in v):
        return "expected [[rank limit, weight 0..1], ...] or null"
    if [r[0] for r in v] != sorted({r[0] for r in v}):
        return "rank limits must be strictly ascending"


def t_bonus(v):
    return None if isinstance(v, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 for x in v) else "expected a list of numbers >= 0 (the bonus for rank 1, 2, ...)"


def t_re(rx, what):
    return lambda v: None if isinstance(v, str) and rx.match(v) else f"expected {what}"


def t_in(*choices):
    return lambda v: None if v in choices else f"expected one of {', '.join(map(str, choices))}"


def t_list(item_check, min_len=0, choices=None):
    def check(v):
        if not isinstance(v, list):
            return "expected a list"
        if len(v) < min_len:
            return f"expected at least {min_len} item(s)"
        for i, x in enumerate(v):
            msg = item_check(x) or (None if choices is None or x in choices else f"expected one of {', '.join(choices)}")
            if msg:
                return f"item {i}: {msg}"
    return check


def t_map(key_check, val_check):
    def check(v):
        if not isinstance(v, dict) or not v:
            return "expected a non-empty object"
        for k, x in v.items():
            msg = key_check(k) or val_check(x)
            if msg:
                return f"{k!r}: {msg}"
    return check


# ---- data model ------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    project: str
    id: str
    type: str
    label: str
    color: str
    priority: int
    enabled: bool
    min_interval_hours: float
    max_items_per_run: int | None
    repo: str                # owner/name; for a discourse source, the site's host name (what links and node ids are keyed by)
    # git
    ref: str | None = None
    paths: tuple = ()
    exclude: tuple = ()
    chunkers: dict = field(default_factory=dict)
    # github
    include: tuple = ()
    since: str | None = None
    # github_releases
    tag_messages: bool = False
    # git_log
    merges: bool = False
    skip_authors: tuple = ()

    @property
    def key(self):
        return f"{self.project}/{self.id}"

    @property
    def kinds(self):
        """The KINDS this source writes."""
        if self.type == "github":
            inc = set(self.include)
            return tuple(k for k, i in (("issue", "issues"), ("pr", "prs"), ("comment", "comments"), ("review", "reviews"), ("summary", "comments")) if i in inc)
        return {"git": ("file",), "git_log": ("commit",), "github_releases": ("release", "tag") if self.tag_messages else ("release",), "discourse": ("topic", "post")}[self.type]


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    description: str
    sources: tuple

    def source(self, sid):
        return next(s for s in self.sources if s.id == sid)


@dataclass(frozen=True)
class Universe:
    id: str
    title: str
    description: str
    default: bool
    projects: tuple


@dataclass(frozen=True)
class Config:
    search: dict
    projects: dict
    universes: dict
    dir: Path

    def default_universe(self):
        return next((u for u in self.universes.values() if u.default), next(iter(self.universes.values()), None))

    def universe(self, uid=None):
        if uid is None:
            return self.default_universe()
        if uid not in self.universes:
            raise KeyError(f"unknown universe {uid!r} (have: {', '.join(self.universes)})")
        return self.universes[uid]

    def universe_sources(self, uid=None):
        return [s for p in self.universe(uid).projects for s in self.projects[p].sources]

    def data_path(self, *parts):
        """Under the data directory: $SEARCH_DATA_DIR if set, else search.json's data_dir (relative to pipelines/search). Each checkout (main, a
        worktree) has its own by default, so experiments never touch the index a running service reads; promote_data.sh moves data across."""
        return (HERE / (os.environ.get("SEARCH_DATA_DIR") or self.search["data_dir"])).joinpath(*parts)

    def project_dir(self, pid):
        return self.data_path("projects", pid)

    def project_db(self, pid):
        return self.project_dir(pid) / "index.db"


# How much a linked document counts when it is pulled in by a top hit (see LINKS.md): per relation as seen from the hit (linkdb.NAMES lists the same names).
RELATED_WEIGHTS = {"closes": 1.0, "closed_by": 1.0, "merged_as": 0.9, "merge_of": 0.9, "shipped_in": 0.3, "ships": 0.2, "mentions": 0.5, "mentioned_by": 0.5,
                   "touches": 0.15, "touched_by": 0.15, "defines": 0.6, "defined_by": 0.6}
SEARCH_FIELDS = {
    "data_dir": (t_str, "data"), "repos_dir": (t_str, "repos"),
    "me": (t_list(t_str), []),                       # who `authors: ["me"]` is: the asker's GitHub login and git author name
    "embedder": (lambda v: None, {}), "reranker": (lambda v: None, {}), "chunking": (lambda v: None, {}),
    "github": (lambda v: None, {}), "discourse": (lambda v: None, {}), "fusion": (lambda v: None, {}), "cache": (lambda v: None, {}), "refresh": (lambda v: None, {}), "llm": (lambda v: None, {}), "neighbours": (lambda v: None, {}), "links": (lambda v: None, {}), "related": (lambda v: None, {}),
}


def _load_json(path, errors):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        errors.append(f"{path.name}: file not found")
    except json.JSONDecodeError as e:
        errors.append(f"{path.name}: invalid JSON: {e}")
    return None


def _search(d, errors):
    v = V("search.json", errors)
    s = v.obj(d, "", SEARCH_FIELDS)
    s["embedder"] = v.obj(d.get("embedder", {}), "embedder", {"model": (t_str, "Qwen/Qwen3-Embedding-0.6B"), "via": (t_in("local", "gateway"), "local"),
                                                              "batch": (t_int(1, 256), 16)}) if isinstance(d, dict) else {}
    s["reranker"] = v.obj(d.get("reranker", {}), "reranker", {"model": (t_str, "Qwen/Qwen3-Reranker-0.6B"), "candidates": (t_int(1, 100), 30),
                                                                    "blend": (t_blend, [[3, 0.75], [10, 0.6], [1000, 0.4]])}) if isinstance(d, dict) else {}
    s["cache"] = v.obj(d.get("cache", {}), "cache", {"enabled": (t_bool, True), "max_entries": (t_int(100), 200000)}) if isinstance(d, dict) else {}
    s["fusion"] = v.obj(d.get("fusion", {}), "fusion", {"k": (t_int(1, 1000), 60), "top_bonus": (t_bonus, [0.05, 0.02, 0.02])}) if isinstance(d, dict) else {}
    s["chunking"] = v.obj(d.get("chunking", {}), "chunking", {"max_chars": (t_int(200, 20000), 2400), "min_chars": (t_int(0, 2000), 20), "pack_chars": (t_int(100, 20000), 1000)}) if isinstance(d, dict) else {}
    s["github"] = v.obj(d.get("github", {}), "github", {"min_remaining": (t_int(0), 300), "page_delay_s": (t_num(0), 0.2), "page_limit": (t_int(1, 100), 90),
                                                        "default_since": (t_re(DATE_RE, "an ISO timestamp like 2000-01-01T00:00:00Z"), "2000-01-01T00:00:00Z")}) if isinstance(d, dict) else {}
    s["discourse"] = v.obj(d.get("discourse", {}), "discourse", {"delay_s": (t_num(0.2), 1.0), "max_retries": (t_int(1, 20), 6), "timeout_s": (t_num(1), 60),
                                                              "user_agent": (t_str, "scala-search-indexer (local research index; https://github.com/retronym)")}) if isinstance(d, dict) else {}
    s["neighbours"] = v.obj(d.get("neighbours", {}), "neighbours", {"enabled": (t_bool, True), "neighbours": (t_int(1, 50), 5), "min_similarity": (t_num(0), 0.8),
                                                                  "clusters": (t_int(1, 1000), 80)}) if isinstance(d, dict) else {}
    s["links"] = v.obj(d.get("links", {}), "links", {"enabled": (t_bool, True), "max_refs_per_chunk": (t_int(1, 5000), 200),
                                                    "github": (lambda x: None, {}),
                                                    "repo_aliases": (lambda x: None if isinstance(x, dict) and all(isinstance(k, str) and REPO_RE.match(k) and isinstance(r, str) and REPO_RE.match(r) for k, r in x.items()) else "expected {old owner/repo: current owner/repo}", {"lampepfl/dotty": "scala/scala3"}),
                                                    "bare_fallbacks": (lambda x: None if isinstance(x, dict) and all(isinstance(k, str) and REPO_RE.match(k) and isinstance(r, list) and all(isinstance(y, str) and REPO_RE.match(y) for y in r) for k, r in x.items()) else "expected {repo: [repos whose issues a bare #N may mean when it is none of the repo's own]}", {"scala/scala": ["scala/bug"]}),
                                                    "legacy_prefixes": (lambda x: None if isinstance(x, dict) and all(isinstance(k, str) and re.match(r"^[A-Z]{2,5}$", k) and isinstance(r, str) and REPO_RE.match(r) for k, r in x.items())
                                                                        else "expected {PREFIX: owner/repo}, e.g. {\"SI\": \"scala/bug\"}", {"SI": "scala/bug"})}) if isinstance(d, dict) else {}
    if isinstance(d, dict):
        s["links"]["github"] = v.obj(d.get("links", {}).get("github", {}) if isinstance(d.get("links"), dict) else {}, "links.github",
                                     {"enabled": (t_bool, True), "max_prs_per_run": (t_int(0), 3000), "batch": (t_int(1, 100), 50), "closing_first": (t_int(1, 100), 10)})
    s["related"] = v.obj(d.get("related", {}), "related", {"enabled": (t_bool, True), "seeds": (t_int(1, 50), 5), "limit": (t_int(1, 50), 8), "per_kind": (t_int(1, 50), 4),
                                                           "depth2": (t_bool, True), "hub_degree": (t_int(1), 150), "boost": (t_num(0), 0.5),
                                                           "weights": (lambda x: None, {})}) if isinstance(d, dict) else {}
    if isinstance(d, dict):
        w = d.get("related", {}).get("weights", {}) if isinstance(d.get("related"), dict) else {}
        s["related"]["weights"] = {**RELATED_WEIGHTS, **(w if isinstance(w, dict) else {})}
        for k, x in (w.items() if isinstance(w, dict) else []):
            if k not in RELATED_WEIGHTS:
                v.err(f"related.weights.{k}", f"unknown relation (have: {', '.join(RELATED_WEIGHTS)})")
            elif isinstance(x, bool) or not isinstance(x, (int, float)) or x < 0:
                v.err(f"related.weights.{k}", "expected a number >= 0")
    r = d.get("refresh", {}) if isinstance(d, dict) else {}
    s["refresh"] = v.obj(r, "refresh", {"at": (t_re(re.compile(r"^([01]\d|2[0-3]):[0-5]\d$"), "HH:MM"), "03:00"), "tiers": (lambda x: None, {}),
                                        "reconcile_every_days": (t_int(1), 7), "budget_hours": (t_num(0, nullable=True), None)})
    tiers = r.get("tiers", {"high": {"max_priority": 3}, "normal": {"max_priority": 6}, "low": {"max_priority": 9}}) if isinstance(r, dict) else {}
    for name, t in (tiers.items() if isinstance(tiers, dict) else []):
        v.obj(t, f"refresh.tiers.{name}", {"max_priority": (t_int(1, 9), REQUIRED)})
    s["refresh"]["tiers"] = tiers
    llm = d.get("llm", {}) if isinstance(d, dict) else {}
    task = lambda name, model, extra: v.obj(llm.get(name, {}) if isinstance(llm, dict) else {}, f"llm.{name}", {"enabled": (t_bool, False), "model": (t_str, model), **extra})
    s["llm"] = {"noise_filter": task("noise_filter", "jevstyle-2b", {}),
                "thread_summaries": task("thread_summaries", "qwen3-coder", {"min_comments": (t_int(2), 8), "max_per_run": (t_int(0), 200)}),
                "digest": {**task("digest", "qwen3-coder", {}), **({} if isinstance(llm, dict) and "digest" in llm else {"enabled": True})},
                "eval_questions": v.obj(llm.get("eval_questions", {}) if isinstance(llm, dict) else {}, "llm.eval_questions",
                                        {"model": (t_str, "qwen3-coder"), "per_project": (t_int(1), 40)})}
    return s


def _source(project, d, v, i, default_since):
    path = f"sources[{i}]"
    if not isinstance(d, dict):
        v.err(path, "expected an object")
        return None
    typ = d.get("type")
    base = {"id": (t_re(ID_RE, "a lowercase id (letters, digits, dashes)"), REQUIRED), "type": (t_in(*SOURCE_TYPES), REQUIRED), "label": (t_str, REQUIRED),
            "color": (t_re(COLOR_RE, "a #rrggbb colour"), "#78716c"), "priority": (t_int(1, 9), 5), "enabled": (t_bool, True),
            "min_interval_hours": (t_num(0), 0), "max_items_per_run": (t_int(1, nullable=True), None), "repo": (t_re(REPO_RE, "owner/name"), REQUIRED)}
    if typ == "discourse":                                       # a forum is named by its host, not a repo
        del base["repo"]
    extra = {"git": {"ref": (t_str, REQUIRED), "paths": (t_list(t_str, 1), REQUIRED), "exclude": (t_list(t_str), []),
                     "chunkers": (t_map(lambda k: None if isinstance(k, str) and k.startswith(".") else "suffix keys start with a dot", t_in(*CHUNKERS)), REQUIRED)},
             "github": {"include": (t_list(t_str, 1, GITHUB_INCLUDE), REQUIRED), "since": (t_re(DATE_RE, "an ISO timestamp like 2020-01-01T00:00:00Z"), default_since)},
             "github_releases": {"tag_messages": (t_bool, False)},
             "git_log": {"ref": (t_str, REQUIRED), "paths": (t_list(t_str), []), "since": (t_re(DATE_RE, "an ISO timestamp like 2018-01-01T00:00:00Z"), default_since),
                         "merges": (t_bool, False), "skip_authors": (t_list(t_str), DEFAULT_SKIP_AUTHORS)},
             "discourse": {"site": (t_re(SITE_RE, "a host name like contributors.scala-lang.org"), REQUIRED),
                           "since": (t_re(DATE_RE, "an ISO timestamp like 2016-01-01T00:00:00Z"), default_since)}}.get(typ, {})
    s = v.obj(d, path, {**base, **extra})
    if typ == "github" and isinstance(d.get("include"), list) and {"comments"} & set(d["include"]) and not {"issues", "prs"} & set(d["include"]):
        v.err(f"{path}.include", "comments need issues and/or prs (they are filtered by what they belong to)")
    if typ == "discourse":
        s["repo"] = s["site"]
    if typ not in SOURCE_TYPES or None in (s["id"], s["repo"]):
        return None
    return Source(project=project, id=s["id"], type=typ, label=s["label"] or s["id"], color=s["color"], priority=s["priority"], enabled=s["enabled"],
                  min_interval_hours=s["min_interval_hours"], max_items_per_run=s["max_items_per_run"], repo=s["repo"], ref=s.get("ref"),
                  paths=tuple(s.get("paths") or ()), exclude=tuple(s.get("exclude") or ()), chunkers=dict(s.get("chunkers") or {}),
                  include=tuple(s.get("include") or ()), since=s.get("since"), tag_messages=bool(s.get("tag_messages")),
                  merges=bool(s.get("merges")), skip_authors=tuple(s.get("skip_authors") or ()))


def _check_overlaps(v, srcs):
    """Two `github` sources of one project on the same repo must not index the same kind of item (it would be stored twice): each of the five
    kinds (issues, PRs, comments on issues, comments on PRs, review comments) goes to at most one source."""
    seen = {}
    for s in srcs:
        if s.type != "github":
            continue
        inc = set(s.include)
        kinds = [k for k, on in (("issues", "issues" in inc), ("prs", "prs" in inc), ("comments on issues", {"comments", "issues"} <= inc),
                                 ("comments on PRs", {"comments", "prs"} <= inc), ("review comments", "reviews" in inc)) if on]
        for k in kinds:
            if (s.repo, k) in seen:
                v.err("sources", f"sources {seen[(s.repo, k)]!r} and {s.id!r} both index {k} of {s.repo}")
            seen[(s.repo, k)] = s.id


def load(config_dir=None):
    """Load and validate everything under `config_dir` ($SEARCH_CONFIG_DIR, else pipelines/search/config). Raises ConfigError listing every problem."""
    cdir = Path(config_dir or os.environ.get("SEARCH_CONFIG_DIR") or CONFIG_DIR)
    errors = []
    raw = _load_json(cdir / "search.json", errors)
    search = _search(raw if raw is not None else {}, errors)
    default_since = search["github"].get("default_since") or "2000-01-01T00:00:00Z"
    projects, universes = {}, {}
    for f in sorted((cdir / "projects").glob("*.json")):
        d = _load_json(f, errors)
        if d is None:
            continue
        v = V(f"projects/{f.name}", errors)
        p = v.obj(d, "", {"id": (t_re(ID_RE, "a lowercase id"), REQUIRED), "title": (t_str, REQUIRED), "sources": (t_list(lambda x: None, 1), REQUIRED)})
        if p["id"] and p["id"] != f.stem:
            v.err("id", f"must match the file name ({f.stem!r})")
        srcs, seen = [], set()
        for i, sd in enumerate(d.get("sources", []) if isinstance(d, dict) and isinstance(d.get("sources"), list) else []):
            s = _source(p["id"] or f.stem, sd, v, i, default_since)
            if s is None:
                continue
            if s.id in seen:
                v.err(f"sources[{i}].id", f"duplicate source id {s.id!r}")
            seen.add(s.id)
            srcs.append(s)
        _check_overlaps(v, srcs)
        if p["id"]:
            projects[p["id"]] = Project(p["id"], p["title"] or p["id"], d.get("description", "") if isinstance(d, dict) else "", tuple(srcs))
    for f in sorted((cdir / "universes").glob("*.json")):
        d = _load_json(f, errors)
        if d is None:
            continue
        v = V(f"universes/{f.name}", errors)
        u = v.obj(d, "", {"id": (t_re(ID_RE, "a lowercase id"), REQUIRED), "title": (t_str, REQUIRED), "default": (t_bool, False),
                          "projects": (t_list(t_str, 1), REQUIRED)})
        if u["id"] and u["id"] != f.stem:
            v.err("id", f"must match the file name ({f.stem!r})")
        for pid in u["projects"] or []:
            if pid not in projects:
                v.err("projects", f"unknown project {pid!r} (have: {', '.join(projects) or 'none'})")
        if u["id"]:
            universes[u["id"]] = Universe(u["id"], u["title"] or u["id"], d.get("description", "") if isinstance(d, dict) else "", bool(u["default"]),
                                          tuple(p for p in (u["projects"] or []) if p in projects))
    if sum(1 for u in universes.values() if u.default) > 1:
        errors.append(f"universes: more than one universe is marked default ({', '.join(u.id for u in universes.values() if u.default)})")
    if not universes and not errors:
        errors.append("universes: none defined")
    if errors:
        raise ConfigError(errors)
    return Config(search, projects, universes, cdir)


def _tree(cfg, only=None):
    for u in cfg.universes.values():
        if only and u.id != only:
            continue
        print(f"universe {u.id}  \"{u.title}\"{'  (default)' if u.default else ''}: {', '.join(u.projects)}")
        for pid in u.projects:
            p = cfg.projects[pid]
            print(f"  project {p.id}  \"{p.title}\"   db: {cfg.project_db(p.id).relative_to(HERE)}")
            for s in sorted(p.sources, key=lambda s: (s.priority, s.id)):
                what = {"git": f"{s.repo}@{s.ref} {','.join(s.paths)} [{','.join(sorted(set(s.chunkers.values())))}]",
                        "github": f"{s.repo} {'+'.join(s.include)} since {(s.since or '')[:10]}", "git_log": f"{s.repo}@{s.ref} commit messages since {(s.since or '')[:10]}{' ' + ','.join(s.paths) if s.paths else ''}", "github_releases": f"{s.repo} releases{' + tag messages' if s.tag_messages else ''}",
                        "discourse": f"{s.repo} forum topics and posts since {(s.since or '')[:10]}"}[s.type]
                caps = ", ".join(x for x in (f"max {s.max_items_per_run}/run" if s.max_items_per_run else "", f"every >= {s.min_interval_hours:g} h" if s.min_interval_hours else "",
                                             "DISABLED" if not s.enabled else "") if x)
                print(f"    p{s.priority} {s.id:9} {s.type:15} {what}{'   (' + caps + ')' if caps else ''}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    try:
        cfg = load()
    except ConfigError as e:
        print(f"{len(e.errors)} configuration problem(s):", file=sys.stderr)
        for m in e.errors:
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)
    if cmd == "check":
        _tree(cfg)
        print(f"OK: {len(cfg.projects)} projects, {sum(len(p.sources) for p in cfg.projects.values())} sources, {len(cfg.universes)} universe(s)")
    elif cmd == "show" and len(sys.argv) > 2:
        _tree(cfg, sys.argv[2])
    else:
        print(__doc__)
