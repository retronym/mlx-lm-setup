#!/usr/bin/env python3
"""Typed links between the documents of a universe, derived from what is already indexed (no model, no network). See LINKS.md.

  links.py [universe] [--force]            rebuild <data>/links/<universe>.db (skipped when no project changed)
  links.py stats [universe]                edges per type, nodes, dangling targets, the biggest hubs
  links.py sample <type> [n] [universe]    n random edges of a type with the words they were found in (judging precision by eye)
  links.py of <ref> [universe]             a node's edges, both directions: `scala/bug#123`, `commit:scala/scala@abc1234`, `file:scala/scala:src/..`

Nodes are documents, not chunks. Ids: `owner/repo#N` (an issue or PR: one number space, `kind` says which when it is indexed), `commit:owner/repo@sha`,
`release:owner/repo@tag`, `file:owner/repo:path`. A target that is not indexed (yet) is a dangling node (`indexed` = 0); the edge is kept and resolves when
the item is backfilled. Edges are directed from the document whose text says it:
  closes     PR / commit -> issue    GitHub's own closing references of a PR (`sources/ghlinks.py`), or a closing keyword (`Fixes #1`) in a PR title or body or a commit message
  merged_as  PR -> commit            the merge or squash commit GitHub recorded
  mentions   any -> issue/PR/commit  `#N`, `owner/repo#N`, URLs, `SI-N`, shas in titles, bodies, comments, review comments, commit messages
  shipped_in issue/PR/commit -> release   the release note (or tag message) names it, or the release's tag is the first, by date, to contain the commit (a PR through its merge commit)
  touches    commit -> file          the paths of a commit message chunk, only to files that are indexed (conf falls with the number of files)
  defines    file -> issue/PR/commit a reference in a comment of the source file
Forum topics (`topic:<host>/<id>`, from a discourse source: the opening post and its replies are one node) mention what their posts link to (GitHub items
and commits by URL, other topics), and are mentioned by anything whose text links to them (a PR that cites the Pre-SIP thread). A bare `#N` in a forum post
names no repo: it is not a reference.
`conf` is the evidence's strength times how sure the target is (a bare `#N` that is no item of the document's own repo but one of its `bare_fallbacks` trackers, as in
scala/scala commits that mean Trac tickets, is a guess). `how` says where the words were found and `snip` quotes them."""
import bisect, json, os, random, re, sqlite3, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, repos, runstate
from refs import make_parser, code_comments

VERSION = "3"
TYPES = ("closes", "merged_as", "mentions", "shipped_in", "touches", "defines")
BASE = {"github": 1.0, "git": 0.95, "title": 0.8, "body": 0.7, "commit msg": 0.7, "comment": 0.5, "review": 0.5, "closing": 0.85, "code comment": 0.9, "release note": 0.9, "tag message": 0.9, "topic": 0.7, "post": 0.5}
_FILES = re.compile(r"\s+and \d+ more$")
_PREFIX = re.compile(r"^\S+#\d+ ")


def db_path(cfg, universe):
    return cfg.data_path("links", f"{universe}.db")


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def _projects(cfg, universe):
    return [(pid, cfg.project_db(pid)) for pid in cfg.universe(universe).projects if cfg.project_db(pid).exists()]


def stamp(cfg, universe):
    """Per project: chunk count and newest write (the same cheap fingerprint as neighbours)."""
    out = []
    for pid, path in _projects(cfg, universe):
        con = _ro(path)
        try:
            out.append([pid, *con.execute("SELECT count(*), max(updated) FROM chunks").fetchone()])
        finally:
            con.close()
    return json.dumps(out)


class Builder:
    def __init__(self, cfg, universe, db, log):
        self.cfg, self.universe, self.db, self.log = cfg, universe, db, log
        lk = cfg.search["links"]
        self.parse = make_parser(lk["legacy_prefixes"], cfg.forums())
        self.max_refs = lk["max_refs_per_chunk"]
        self.alias, self.fallbacks = lk["repo_aliases"], lk["bare_fallbacks"]
        self.items = set()                  # indexed `owner/repo#N`
        self.commits = []                   # sorted shas of indexed commits, for short-sha prefix matching
        self.commit_repo = {}
        self.files = set()
        self.skipped_touches = 0
        self.merged = {}                    # merged PR node -> merge commit sha

    # ---- pass 1: the nodes that exist ------------------------------------------------------------------------------------------
    def source(self, pid, sid):
        try:
            return self.cfg.projects[pid].source(sid)
        except StopIteration:
            return None

    def node(self, id, kind, project, repo, ref, title="", state=None, created=None, url=None, indexed=1, chunk=None):
        self.db.execute("INSERT OR IGNORE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?)", (id, kind, project, repo, ref, title, state, created, url, indexed, chunk))

    def scan_nodes(self):
        commits = []
        for pid, path in _projects(self.cfg, self.universe):
            con = _ro(path)
            try:
                for cid, sid, title, url, meta, doc in con.execute("SELECT id, source, title, url, meta, doc FROM chunks WHERE id NOT LIKE '%~%'"):
                    src = self.source(pid, sid)
                    if src is None:
                        continue
                    m = json.loads(meta or "{}")
                    kind = m.get("kind") or "file"
                    if kind in ("issue", "pr"):
                        nid = f"{src.repo}#{m['number']}"
                        self.items.add(nid)
                        self.node(nid, kind, pid, src.repo, str(m["number"]), _PREFIX.sub("", title), m.get("state"), m.get("created"), url, chunk=cid)
                    elif kind == "commit":
                        commits.append(m["sha"]); self.commit_repo[m["sha"]] = src.repo
                        self.node(f"commit:{src.repo}@{m['sha']}", "commit", pid, src.repo, m["sha"], title, None, m.get("created"), url, chunk=cid)
                    elif kind == "topic" and "~" not in cid:
                        self.node(f"topic:{src.repo}/{m['topic']}", "topic", pid, src.repo, str(m["topic"]), title, None, m.get("created"), url, chunk=cid)
                    elif kind in ("release", "tag"):
                        if m.get("tag"):
                            self.node(f"release:{src.repo}@{m['tag']}", "release", pid, src.repo, m["tag"], title, None, m.get("published"), url, chunk=cid)
                for sid, doc, cid in con.execute("SELECT source, doc, min(id) FROM chunks WHERE json_extract(meta, '$.kind') IS NULL GROUP BY source, doc"):
                    src = self.source(pid, sid)
                    if src is not None and src.type == "git":
                        self.files.add(f"file:{src.repo}:{doc}")
                        self.node(f"file:{src.repo}:{doc}", "file", pid, src.repo, doc, doc, chunk=cid)
            finally:
                con.close()
        self.commits = sorted(set(commits))
        self.log(f"links: {len(self.items)} issues and PRs, {len(self.commits)} commits, {len(self.files)} files")

    # ---- resolving a reference to a node ---------------------------------------------------------------------------------------
    def resolve(self, ref, ctx_repo):
        """[(node id, certainty)]: usually one; a bare number that is no item of the document's own repo may be one of another tracker's."""
        if ref.kind == "commit":
            sha = ref.key
            if len(sha) < 40:
                i = bisect.bisect_left(self.commits, sha)
                hits = [s for s in self.commits[i:i + 2] if s.startswith(sha)]
                if len(hits) != 1:
                    return []                                              # unknown or ambiguous short sha
                sha = hits[0]
            repo = self.commit_repo.get(sha) or (self.alias.get(ref.repo, ref.repo) if ref.via == "url" else None)
            return [(f"commit:{repo}@{sha}", 1.0)] if repo else []         # a bare sha that is no indexed commit could be of any repo: dropped
        if ref.kind == "topic":
            return [(f"topic:{ref.repo}/{ref.key}", 1.0)]
        if ref.repo:
            return [(f"{self.alias.get(ref.repo, ref.repo)}#{ref.key}", 1.0)]
        if ctx_repo is None:
            return []                                                      # a bare #N in a forum post: no repo to read it against
        own = f"{ctx_repo}#{ref.key}"
        if own in self.items:
            return [(own, 1.0)]
        others = [f"{r}#{ref.key}" for r in self.fallbacks.get(ctx_repo, []) if f"{r}#{ref.key}" in self.items]
        if others:
            return [(o, 0.5 if len(others) == 1 else 0.3) for o in others]
        return [(own, 1.0)]

    def edge(self, src, dst, type, conf, how, snip):
        if src == dst:
            return
        self.db.execute("""INSERT INTO edges VALUES(?,?,?,?,?,?) ON CONFLICT(src, dst, type) DO UPDATE SET conf = excluded.conf, how = excluded.how, snip = excluded.snip
                           WHERE excluded.conf > conf""", (src, dst, type, round(conf, 3), how, snip))

    def dangling(self, nid):
        if nid in self.items:
            return
        if nid.startswith("commit:"):
            repo, sha = nid[7:].split("@", 1)
            self.node(nid, "commit", None, repo, sha, indexed=0)
        elif nid.startswith("topic:"):
            host, tid = nid[6:].split("/", 1)
            self.node(nid, "topic", None, host, tid, url=f"https://{host}/t/{tid}", indexed=0)
        else:
            repo, n = nid.split("#", 1)
            self.node(nid, "item", None, repo, n, indexed=0)

    def refs_of(self, text, src_node, ctx_repo, how, type, closing_ok, flip=False, lists=True):
        """Add the edges for every reference in `text`. `flip`: the document is the *target* of the edge (a release note ships what it names)."""
        for ref in self.parse(text, lists)[:self.max_refs]:
            for dst, certainty in self.resolve(ref, ctx_repo):
                t = type
                if closing_ok and ref.closing and ref.kind == "issue":
                    t, base = "closes", BASE["closing"]
                else:
                    base = BASE[how]
                if dst != src_node:
                    self.dangling(dst)
                fl = flip
                if flip and ref.kind == "topic":                           # a release note linking its forum announcement mentions it; it does not ship it
                    t, fl = "mentions", False
                a, b = (dst, src_node) if fl else (src_node, dst)
                self.edge(a, b, t, base * certainty, how, ref.snip)

    # ---- pass 2: the edges ------------------------------------------------------------------------------------------------------
    def scan_edges(self):
        n = 0
        for pid, path in _projects(self.cfg, self.universe):
            con = _ro(path)
            try:
                for cid, sid, title, text, meta, doc in con.execute("SELECT id, source, title, text, meta, doc FROM chunks"):
                    src = self.source(pid, sid)
                    if src is None:
                        continue
                    m = json.loads(meta or "{}")
                    kind, repo = m.get("kind") or "file", src.repo
                    if kind in ("issue", "pr", "comment", "review"):
                        thread = f"{repo}#{m['number']}"
                        if kind in ("issue", "pr"):
                            if kind == "pr" and "~" not in cid:
                                self.github_edges(thread, repo, m)
                            if "~" not in cid:
                                self.refs_of(_PREFIX.sub("", title), thread, repo, "title", "mentions", kind == "pr", lists=False)
                            self.refs_of(text, thread, repo, "body", "mentions", kind == "pr", lists=False)       # a PR body is read as GitHub reads it
                        else:
                            self.refs_of(text, thread, repo, kind if kind == "review" else "comment", "mentions", False)
                    elif kind == "commit":
                        node = f"commit:{repo}@{m['sha']}"
                        msg, _, files = text.rpartition("\n\nFiles changed: ")
                        if not msg:
                            msg, files = text, ""
                        self.refs_of(msg, node, repo, "commit msg", "mentions", True)
                        if files:
                            self.touches(node, repo, _FILES.sub("", files.strip()).split(", "), m.get("files") or 1)
                    elif kind in ("release", "tag"):
                        if m.get("tag"):
                            node = f"release:{repo}@{m['tag']}"
                            self.refs_of(text, node, repo, "release note" if kind == "release" else "tag message", "shipped_in", False, flip=True)
                    elif kind in ("topic", "post"):
                        self.refs_of(text + "\n" + "\n".join(m.get("urls") or []), f"topic:{repo}/{m['topic']}", None, kind, "mentions", False)
                    elif kind == "file" and src.type == "git":
                        self.refs_of(code_comments(text), f"file:{repo}:{doc}", repo, "code comment", "defines", False)
                    n += 1
                    if n % 20000 == 0:
                        self.db.commit()
            finally:
                con.close()
        self.ship_from_tags()
        self.db.execute("DELETE FROM edges WHERE type = 'mentions' AND EXISTS (SELECT 1 FROM edges e WHERE e.src = edges.src AND e.dst = edges.dst AND e.type = 'closes')")
        self.db.commit()

    def github_edges(self, pr, repo, m):
        """What GitHub knows (meta `closes`, `merge_sha`, written by sources/ghlinks.py): the real closing references and the merge commit."""
        for target in m.get("closes") or []:
            dst = f"{self.alias.get(target.split('#')[0], target.split('#')[0])}#{target.split('#')[1]}"
            self.dangling(dst)
            self.edge(pr, dst, "closes", BASE["github"], "github closing ref", target)
        sha = m.get("merge_sha")
        if sha:
            dst = f"commit:{self.commit_repo.get(sha, repo)}@{sha}"
            self.dangling(dst)
            self.edge(pr, dst, "merged_as", BASE["github"], "github merge commit", sha[:10])
            self.merged[pr] = sha

    def ship_from_tags(self):
        """A commit shipped in the first release, by tag date, whose tag contains it (and a merged PR with it, through its merge commit). One
        `git rev-list` per tag against the tags before it, over the managed clones; only tags that have a release node count."""
        rels = {}
        for nid, repo, tag in self.db.execute("SELECT id, repo, ref FROM nodes WHERE kind = 'release' AND indexed = 1"):
            rels.setdefault(repo, {})[tag] = nid
        pr_of = {}
        for pr, sha in self.merged.items():
            pr_of.setdefault(sha, []).append(pr)
        for repo, tags in rels.items():
            clone = repos.path_for(self.cfg, repo)
            if not clone.exists():
                continue
            order = subprocess.run(["git", "-C", str(clone), "for-each-ref", "--sort=creatordate", "--format=%(refname:short)", "refs/tags"], capture_output=True, text=True).stdout.split()
            seen, n = [], 0
            for tag in order:
                new = subprocess.run(["git", "-C", str(clone), "rev-list", "--stdin"], input="".join(f"^{s}\n" for s in seen) + f"{tag}\n", capture_output=True, text=True).stdout.split()
                seen.append(tag)
                if tag not in tags:
                    continue
                for sha in new:
                    srcs = ([f"commit:{self.commit_repo[sha]}@{sha}"] if sha in self.commit_repo else []) + pr_of.get(sha, [])
                    for src in srcs:
                        self.edge(src, tags[tag], "shipped_in", BASE["git"], "first tag containing the commit", tag)
                        n += 1
            self.log(f"links: {repo}: {n} commit and PR to release edges from {len(order)} tags")

    def touches(self, commit, repo, paths, nfiles):
        conf = 1.0 if nfiles <= 5 else max(0.1, round(5 / nfiles, 2))
        for p in paths:
            f = f"file:{repo}:{p}"
            if f in self.files:
                self.edge(commit, f, "touches", conf, "commit files", f"{nfiles} files")
            else:
                self.skipped_touches += 1


def build(cfg, universe, path, log):
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("""CREATE TABLE nodes(id TEXT PRIMARY KEY, kind TEXT, project TEXT, repo TEXT, ref TEXT, title TEXT, state TEXT, created TEXT, url TEXT, indexed INTEGER, chunk TEXT);
                        CREATE TABLE edges(src TEXT, dst TEXT, type TEXT, conf REAL, how TEXT, snip TEXT, PRIMARY KEY(src, dst, type));
                        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);""")
    b = Builder(cfg, universe, db, log)
    b.scan_nodes()
    b.scan_edges()
    db.executescript("CREATE INDEX edges_dst ON edges(dst, type); CREATE INDEX edges_src ON edges(src, type);")
    counts = dict(db.execute("SELECT type, count(*) FROM edges GROUP BY type"))
    nodes, dangling = db.execute("SELECT count(*), sum(indexed = 0) FROM nodes").fetchone()
    db.executemany("INSERT INTO meta VALUES(?,?)", {"version": VERSION, "stamp": stamp(cfg, universe), "params": json.dumps(cfg.search["links"], sort_keys=True), "generated": str(time.time()),
                                                    "nodes": str(nodes), "dangling": str(dangling or 0), "edges": json.dumps(counts), "skipped_touches": str(b.skipped_touches)}.items())
    db.commit()
    db.close()
    tmp.replace(path)
    return {"nodes": nodes, "dangling": dangling or 0, "edges": counts}


def compute(cfg, universe, run=None, force=False):
    """Rebuild the links database of `universe`. Returns a summary dict, or None when nothing changed."""
    log = run.log if run else print
    path = db_path(cfg, universe)
    cur, params = stamp(cfg, universe), json.dumps(cfg.search["links"], sort_keys=True)
    if path.exists() and not force:
        try:
            con = _ro(path)
            meta = dict(con.execute("SELECT k, v FROM meta"))
            con.close()
            if meta.get("stamp") == cur and meta.get("params") == params and meta.get("version") == VERSION:
                log("links: no new or changed chunks")
                return None
        except sqlite3.Error:
            pass
    t0 = time.time()
    s = build(cfg, universe, path, log)
    log(f"links: {sum(s['edges'].values())} edges ({', '.join(f'{k} {v}' for k, v in sorted(s['edges'].items()))}), {s['nodes']} nodes ({s['dangling']} not indexed) in {time.time() - t0:.0f} s")
    return s


# ---- reading ---------------------------------------------------------------------------------------------------------------------
def stats(cfg, universe, out=print):
    con = _ro(db_path(cfg, universe))
    meta = dict(con.execute("SELECT k, v FROM meta"))
    out(f"links for {universe}: {meta['nodes']} nodes ({meta['dangling']} not indexed), built {time.strftime('%Y-%m-%d %H:%M', time.localtime(float(meta['generated'])))}; "
        f"{meta['skipped_touches']} commit/file paths skipped (file not indexed)")
    out("edges by type (count, mean conf, share of targets not indexed):")
    for t, n, c, d in con.execute("""SELECT e.type, count(*), avg(e.conf), avg(n.indexed = 0) FROM edges e JOIN nodes n ON n.id = e.dst GROUP BY e.type ORDER BY 2 DESC"""):
        out(f"  {t:11} {n:8}  conf {c:.2f}  dangling {d:.0%}")
    out("nodes by kind (indexed / not):")
    for k, a, b in con.execute("SELECT kind, sum(indexed), sum(indexed = 0) FROM nodes GROUP BY kind ORDER BY 2 DESC"):
        out(f"  {k:8} {a:8} {b:8}")
    for label, col, other in (("most referenced (in-degree)", "dst", "src"), ("references most (out-degree)", "src", "dst")):
        out(label + ":")
        for nid, n, title in con.execute(f"SELECT e.{col}, count(*) c, COALESCE((SELECT title FROM nodes WHERE id = e.{col}), '') FROM edges e GROUP BY e.{col} ORDER BY c DESC LIMIT 8"):
            out(f"  {n:6}  {nid}  {' '.join((title or '').split())[:70]}")


def sample(cfg, universe, type, n, out=print, seed=None):
    con = _ro(db_path(cfg, universe))
    rows = con.execute("SELECT e.src, e.dst, e.conf, e.how, e.snip, n.indexed FROM edges e JOIN nodes n ON n.id = e.dst WHERE e.type = ?", (type,)).fetchall()
    for src, dst, conf, how, snip, indexed in random.Random(seed).sample(rows, min(n, len(rows))):
        out(f"{src} -{type}-> {dst}{'' if indexed else ' (not indexed)'}  conf {conf}  [{how}]  ...{snip}...")


def of(cfg, universe, ref, out=print):
    con = _ro(db_path(cfg, universe))
    title = lambda i: " ".join(((con.execute("SELECT title FROM nodes WHERE id = ?", (i,)).fetchone() or [""])[0] or "").split())[:70]
    for src, dst, type, conf, how in con.execute("SELECT src, dst, type, conf, how FROM edges WHERE src = ? ORDER BY type, conf DESC", (ref,)):
        out(f"  -{type}-> {dst}  {title(dst)}  conf {conf} [{how}]")
    for src, dst, type, conf, how in con.execute("SELECT src, dst, type, conf, how FROM edges WHERE dst = ? ORDER BY type, conf DESC LIMIT 100", (ref,)):
        out(f"  <-{type}- {src}  {title(src)}  conf {conf} [{how}]")


def main(argv):
    a = [x for x in argv if not x.startswith("--")]
    cfg = config.load()
    cmd = a[0] if a and a[0] in ("stats", "sample", "of") else None
    rest = a[1:] if cmd else a
    if cmd == "sample":
        type, rest = rest[0], rest[1:]
        n = int(rest.pop(0)) if rest and rest[0].isdigit() else 20
    if cmd == "of":
        ref, rest = rest[0], rest[1:]
    universe = rest[0] if rest else cfg.default_universe().id
    cfg.universe(universe)
    if cmd == "stats":
        stats(cfg, universe)
    elif cmd == "sample":
        sample(cfg, universe, type, n)
    elif cmd == "of":
        of(cfg, universe, ref)
    else:
        try:
            with runstate.lock(cfg):
                run = runstate.Run(cfg, "links", [])
                compute(cfg, universe, run, force="--force" in argv)
                run.finish()
        except runstate.Busy as e:
            print(e, file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
