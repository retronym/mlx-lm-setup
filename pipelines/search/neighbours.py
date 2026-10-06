#!/usr/bin/env python3
"""Nearest-neighbour pairs, topic clusters and outlier scores over the documents of a universe, from vectors already in the indexes (numpy only; no model).

  neighbours.py [universe] [--force]

Items are documents, one per issue, PR, commit, release, tag, forum topic and file: the vector of its first chunk (a release's header, a topic's opening
post), or for a file the normalised mean of its chunks. Comments, review comments and forum replies are part of their thread, not items.

Writes <data>/neighbours/<universe>.db, which the gateway reads for the Duplicates, Clusters and Outliers tabs:
  items(idx, ..., cluster, iso, ctr)                                                  iso = cosine to the nearest other item OF THE SAME GROUP (issues and PRs; commits;
                                                                                      files; releases and tags; forum topics), ctr = cosine to its cluster centre (low = outlier).
                                                                                      Per group, because across kinds a merged PR's nearest item is its own squash commit.
  pairs(a, b, sim)                                                                    issues and PRs only (the Duplicates tab): a < b; each item's `neighbours` best matches >= `min_similarity`
  clusters(k, label, samples)                                                         spherical k-means over every item, so a topic gathers issues, PRs, commits, code and
                                                                                      forum threads; label = distinctive title terms, samples = idx closest to the centre
  meta(k, v)                                                                          model, stamp, counts

Everything that depends on a filter (state, kind, dates, repo, the old-import adjacency rule) is applied by the reader, so one run serves every
view. Skipped when no project has new or changed item vectors since the last run (`--force` overrides)."""
import json, math, os, re, sqlite3, sys, time
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import config, runstate

BLOCK = 1024
VERSION = "3"
GROUP = {"issue": "issues", "pr": "issues", "commit": "commits", "file": "files", "release": "releases", "tag": "releases", "topic": "topics"}
# the chunks that stand for a document: an issue's or PR's body, a commit, a release's header chunk, a tag, a forum topic's opening post (files are averaged)
HEADS = ("(json_extract(c.meta, '$.kind') IN ('issue', 'pr', 'commit', 'tag', 'topic') AND c.id NOT LIKE '%~%' "
         "OR json_extract(c.meta, '$.kind') = 'release' AND c.id LIKE '%:header')")
STOP = set("the a an of in on to for and or with is not be by as at from when using use fix add remove update support error warning scala scalac compiler "
           "code should does doesn don it its this that are was can cannot no new bug issue pr via into after before than more only also".split())


def db_path(cfg, universe):
    return cfg.data_path("neighbours", f"{universe}.db")


def load_items(cfg, universe):
    """[item dict], float32 unit vectors: every document of the universe that has current vectors (see the module doc)."""
    model = cfg.search["embedder"]["model"]
    items, vecs = [], []
    for pid in cfg.universe(universe).projects:
        path = cfg.project_db(pid)
        if not path.exists():
            continue
        git = {s.id for s in cfg.projects[pid].sources if s.type == "git"}
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        try:
            for cid, title, url, meta, v in con.execute(f"""SELECT c.id, c.title, c.url, c.meta, v.v FROM chunks c JOIN vec v ON v.rowid = c.rowid AND v.hash = c.hash AND v.model = ?
                                                            WHERE {HEADS}""", (model,)):
                m = json.loads(meta or "{}")
                items.append({"project": pid, "id": cid, "kind": m["kind"], "state": m.get("state"), "created": m.get("created") or m.get("published"), "number": m.get("number"),
                              "author": m.get("author") or m.get("author_name"), "title": title, "url": url})
                vecs.append(np.frombuffer(v, dtype=np.float16).astype(np.float32))
            cur, acc = None, None                                # files: the mean of their chunks, in (source, doc) order
            for sid, doc, cid, url, v in con.execute("""SELECT c.source, c.doc, c.id, c.url, v.v FROM chunks c JOIN vec v ON v.rowid = c.rowid AND v.hash = c.hash AND v.model = ?
                                                        WHERE json_extract(c.meta, '$.kind') IS NULL ORDER BY c.source, c.doc, c.rowid""", (model,)):
                if sid not in git:
                    continue
                if cur != (sid, doc):
                    if acc is not None:
                        vecs.append(acc / (np.linalg.norm(acc) or 1))
                    cur, acc = (sid, doc), np.zeros(len(v) // 2, dtype=np.float32)
                    items.append({"project": pid, "id": cid, "kind": "file", "state": None, "created": None, "number": None, "author": None,
                                  "title": doc, "url": (url or "").split("#")[0]})
                acc += np.frombuffer(v, dtype=np.float16)
            if acc is not None:
                vecs.append(acc / (np.linalg.norm(acc) or 1))
        finally:
            con.close()
    return items, (np.vstack(vecs) if vecs else np.zeros((0, 1), dtype=np.float32))


def stamp(cfg, universe):
    """What the items look like to this module, cheap to compute: per project, vectors for items and the newest chunk write."""
    model, out = cfg.search["embedder"]["model"], []
    for pid in cfg.universe(universe).projects:
        path = cfg.project_db(pid)
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        try:
            n, newest, states = con.execute(f"""SELECT count(*), max(c.updated), sum(json_extract(c.meta, '$.state') = 'open') FROM chunks c JOIN vec v ON v.rowid = c.rowid AND v.hash = c.hash AND v.model = ?
                                                WHERE {HEADS} OR json_extract(c.meta, '$.kind') IS NULL""", (model,)).fetchone()
        finally:
            con.close()
        out.append([pid, n, newest, states])
    return json.dumps(out)


def neighbour_pairs(X, k, min_sim):
    """({(a, b): cosine} with a < b: the k best matches of every row, kept if >= min_sim; for every row the cosine to its nearest other row)."""
    n, pairs = len(X), {}
    nearest = np.zeros(n, dtype=np.float32)
    if n < 2:
        return pairs, nearest
    k = min(k, n - 1)
    for a in range(0, n, BLOCK):
        s = X[a:a + BLOCK] @ X.T
        s[np.arange(s.shape[0]), a + np.arange(s.shape[0])] = -1
        nearest[a:a + BLOCK] = s.max(1)
        top = np.argpartition(-s, k - 1, axis=1)[:, :k]
        for r in range(s.shape[0]):
            for j in top[r]:
                if s[r, j] >= min_sim:
                    key = (a + r, int(j)) if a + r < j else (int(j), a + r)
                    pairs[key] = max(pairs.get(key, 0.0), float(s[r, j]))
    return pairs, nearest


def kmeans(X, k, seed=0, iters=50):
    """Spherical k-means (k-means++ start). Returns (assignment, similarity to the assigned centre). X rows are unit vectors."""
    n = len(X)
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    idx = [int(rng.integers(n))]
    d = 1 - X @ X[idx[0]]
    for _ in range(k - 1):
        p = np.clip(d, 0, None) ** 2
        idx.append(int(rng.choice(n, p=p / p.sum())) if p.sum() > 0 else int(rng.integers(n)))
        d = np.minimum(d, 1 - X @ X[idx[-1]])
    C = X[idx].copy()
    for _ in range(iters):
        a = (X @ C.T).argmax(1)
        new = np.zeros_like(C)
        np.add.at(new, a, X)
        for j in np.nonzero(np.bincount(a, minlength=k) == 0)[0]:
            new[j] = X[int(rng.integers(n))]
        new /= np.linalg.norm(new, axis=1, keepdims=True) + 1e-12
        moved = float(np.linalg.norm(new - C))
        C = new
        if moved < 1e-4:
            break
    s = X @ C.T
    return s.argmax(1), s.max(1)


def nearest(X):
    """For every row, the cosine to its nearest other row."""
    n = len(X)
    out = np.zeros(n, dtype=np.float32)
    if n < 2:
        return out
    for a in range(0, n, BLOCK):
        s = X[a:a + BLOCK] @ X.T
        s[np.arange(s.shape[0]), a + np.arange(s.shape[0])] = -1
        out[a:a + BLOCK] = s.max(1)
    return out


_PREFIX = re.compile(r"^(?:\S+#\d+ |\S+ commit [0-9a-f]{7,40} |\S+ (?:release|tag) )")


def tokens(title):
    t = re.sub(r"\s+\((?:reply #\d+|comment|review comment).*\)$", "", _PREFIX.sub("", title)).lower()
    return {w for w in re.findall(r"[a-z][a-z0-9_.]{2,}", t) if w not in STOP}


def labels(items, assign, k):
    """The four most distinctive title terms of each cluster (term frequency x inverse cluster frequency); a term must occur in >= 3 titles."""
    per = [Counter() for _ in range(k)]
    for it, c in zip(items, assign):
        per[c].update(tokens(it["title"]))
    df = Counter(w for cnt in per for w in cnt)
    out = []
    for cnt in per:
        sc = {w: f * math.log(1 + k / df[w]) for w, f in cnt.items() if f >= 3}
        out.append(", ".join(w for w, _ in sorted(sc.items(), key=lambda x: (-x[1], x[0]))[:4]))
    return out


def compute(cfg, universe, run=None, force=False):
    """Rebuild the neighbours database of `universe`. Returns a summary dict, or None when nothing changed."""
    log = run.log if run else print
    cur = stamp(cfg, universe)
    path = db_path(cfg, universe)
    model = cfg.search["embedder"]["model"]
    nb = cfg.search["neighbours"]
    if path.exists() and not force:
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            meta = dict(con.execute("SELECT k, v FROM meta"))
            con.close()
            if meta.get("stamp") == cur and meta.get("model") == model and meta.get("params") == json.dumps(nb, sort_keys=True) and meta.get("version") == VERSION:
                log("neighbours: no new or changed document vectors")
                return None
        except sqlite3.Error:
            pass
    t0 = time.time()
    items, X = load_items(cfg, universe)
    n = len(items)
    log(f"neighbours: {n} documents ({', '.join(f'{k} {v}' for k, v in Counter(it['kind'] for it in items).most_common())})")
    iso, pairs = np.zeros(n, dtype=np.float32), {}
    for g in sorted(set(GROUP.values())):
        rows = np.array([i for i, it in enumerate(items) if GROUP[it["kind"]] == g], dtype=int)
        if not len(rows):
            continue
        t = time.time()
        if g == "issues":                                        # duplicate candidates are issues and PRs only
            p, iso[rows] = neighbour_pairs(X[rows], nb["neighbours"], nb["min_similarity"])
            pairs = {(int(rows[a]), int(rows[b])): v for (a, b), v in p.items()}
        else:
            iso[rows] = nearest(X[rows])
        log(f"neighbours: nearest within {g} ({len(rows)}) in {time.time() - t:.0f} s")
    k = max(1, min(nb["clusters"], n // 10)) if n else 0
    assign, best = kmeans(X, k) if n else (np.zeros(0, dtype=int), np.zeros(0))
    names = labels(items, assign, k) if n else []
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""CREATE TABLE items(idx INTEGER PRIMARY KEY, project TEXT, id TEXT, kind TEXT, state TEXT, created TEXT, number INTEGER, author TEXT, title TEXT, url TEXT, cluster INTEGER, iso REAL, ctr REAL);
                         CREATE TABLE pairs(a INTEGER, b INTEGER, sim REAL, PRIMARY KEY(a, b));
                         CREATE INDEX pairs_sim ON pairs(sim DESC);
                         CREATE TABLE clusters(k INTEGER PRIMARY KEY, label TEXT, samples TEXT);
                         CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);""")
    con.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(i, it["project"], it["id"], it["kind"], it["state"], it["created"], it["number"], it["author"], it["title"], it["url"], int(assign[i]),
                      round(float(iso[i]), 4), round(float(best[i]), 4)) for i, it in enumerate(items)])
    con.executemany("INSERT INTO pairs VALUES(?,?,?)", [(a, b, round(s, 4)) for (a, b), s in pairs.items()])
    for c in range(k):
        members = np.nonzero(assign == c)[0]
        con.execute("INSERT INTO clusters VALUES(?,?,?)", (c, names[c], json.dumps([int(i) for i in members[np.argsort(-best[members])][:8]])))
    meta = {"version": VERSION, "model": model, "stamp": cur, "params": json.dumps(nb, sort_keys=True), "generated": str(time.time()), "items": str(n), "pairs": str(len(pairs)), "clusters": str(k)}
    con.executemany("INSERT INTO meta VALUES(?,?)", meta.items())
    con.commit()
    con.close()
    tmp.replace(path)
    log(f"neighbours: {len(pairs)} pairs, {k} clusters in {time.time() - t0:.0f} s")
    return {"items": n, "pairs": len(pairs), "clusters": k}


def main(argv):
    force = "--force" in argv
    names = [a for a in argv if not a.startswith("--")]
    cfg = config.load()
    universe = names[0] if names else cfg.default_universe().id
    cfg.universe(universe)
    try:
        with runstate.lock(cfg):
            run = runstate.Run(cfg, "neighbours", [])
            compute(cfg, universe, run, force=force)
            run.finish()
    except runstate.Busy as e:
        print(e, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
