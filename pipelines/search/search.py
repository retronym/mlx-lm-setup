#!/usr/bin/env python3
"""Hybrid search over a universe: every member project has its own database; each is queried with BM25 and (when the query is embedded)
by vector cosine, the per-store hits are merged by score into one keyword list and one vector list (cosine is comparable across
projects because a universe shares one embedding model), the two lists are fused by reciprocal rank, and the best candidates are
optionally reranked by a cross-encoder.

usage: search.py [--universe U] [--project P]... [--source S]... [--kind K]... [-k 8] [--open] [--bm25|--vec] [--rerank] <query>
       (a --source is an id like `issues`, or `project/source`; a --kind is one of store.KINDS, like `commit` or `file`)"""
import json, re, sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
import config
from store import Store, _CAMEL, STATE_SQL, chunk_filter

STOP = set("a an the of in on to is are was be for and or not with how what where why does do this that it as by from at".split())


class Index:
    """The existing databases of one universe's projects (a project that has not been indexed yet is listed in `missing`)."""

    def __init__(self, cfg, universe_id=None):
        self.cfg, self.universe = cfg, cfg.universe(universe_id)
        self.stores, self.missing = {}, []
        for pid in self.universe.projects:
            db = cfg.project_db(pid)
            (self.stores.__setitem__(pid, Store(db)) if db.exists() else self.missing.append(pid))

    def source(self, pid, sid):
        return self.cfg.projects[pid].source(sid)


def fts_query(q):
    """OR of the query's words and their camelCase parts."""
    terms = []
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", q):
        for t in [w] + (_CAMEL.split(w) if len(w) > 3 else []):
            t = t.lower()
            if len(t) > 1 and t not in STOP and t not in terms:
                terms.append(t)
    return " OR ".join(f'"{t}"' for t in terms)


def bm25(st, q, k, sources=None, open_only=False, kinds=None):
    """[(rowid, bm25 score)] best first (FTS5 scores are negative: lower is better)."""
    fq = fts_query(q)
    if not fq:
        return []
    cond, args = chunk_filter(sources, open_only, kinds)
    sql = f"SELECT c.rowid, bm25(fts, 3.0, 1.0) s FROM fts JOIN chunks c ON c.rowid = fts.rowid WHERE fts MATCH ? {cond} ORDER BY s LIMIT ?"
    return [(r[0], r[1]) for r in st.db.execute(sql, (fq, *args, k))]


def fuse(rankings, k=60):
    score = {}
    for r in rankings:
        for i, key in enumerate(r):
            score[key] = score.get(key, 0) + 1 / (k + i + 1)
    return sorted(score, key=score.get, reverse=True)


def _source_filter(sources, pid):
    """Which source ids of project `pid` a `sources` filter ([id | project/source]) selects: None = all, [] = none."""
    if not sources:
        return None
    return sorted({s.split("/", 1)[1] if "/" in s else s for s in sources if "/" not in s or s.split("/", 1)[0] == pid})


def search(idx, q, k=8, projects=None, sources=None, mode="hybrid", open_only=False, embedder=None, reranker=None, pool_docs=30, kinds=None):
    """Ranked [(project, rowid)] plus per-hit detail {(project, rowid): {"bm25": rank, "vec": rank, "rerank": score}} (ranks are 1-based)."""
    pool = max(50, k * 5)
    stores = {p: s for p, s in idx.stores.items() if not projects or p in projects}
    rankings, names = [], []
    if mode in ("hybrid", "bm25"):
        hits = []
        for pid, st in stores.items():
            f = _source_filter(sources, pid)
            if f == []:
                continue
            hits += [(score, pid, rid) for rid, score in bm25(st, q, pool, f, open_only, kinds)]
        rankings.append([(pid, rid) for _, pid, rid in sorted(hits)[:pool]]), names.append("bm25")
    if mode in ("hybrid", "vec") and embedder is not None:
        from embed import vector_search
        qvec, hits = embedder.query(q), []
        for pid, st in stores.items():
            f = _source_filter(sources, pid)
            if f == []:
                continue
            hits += [(-cos, pid, rid) for rid, cos in vector_search(st, qvec, embedder.name, pool, f, open_only, kinds)]
        rankings.append([(pid, rid) for _, pid, rid in sorted(hits)[:pool]]), names.append("vec")
    detail = {}
    for name, r in zip(names, rankings):
        for i, key in enumerate(r):
            detail.setdefault(key, {})[name] = i + 1
    doc_of = {}
    def doc(key):
        if key not in doc_of:
            pid, rid = key
            doc_of[key] = (pid, *stores[pid].db.execute("SELECT source, doc FROM chunks WHERE rowid=?", (rid,)).fetchone())
        return doc_of[key]
    seen, out = set(), []                      # one hit per document: the best chunk of an issue, file or page
    for key in fuse(rankings):
        if doc(key) not in seen:
            seen.add(doc(key))
            out.append(key)
    if reranker is not None and out:
        cand = out[:pool_docs]
        for pid in stores:                     # a small project's best hit must reach the reranker even when big projects crowd the top
            best = next((key for key in out if key[0] == pid), None)
            if best is not None and best not in cand:
                cand.append(best)
        docs = [" ".join(stores[pid].db.execute("SELECT title, text FROM chunks WHERE rowid=?", (rid,)).fetchone()) for pid, rid in cand]
        sc = reranker.scores(q, docs)
        for key, x in zip(cand, sc):
            detail[key]["rerank"] = round(x, 4)
        out = [key for key, _ in sorted(zip(cand, sc), key=lambda x: -x[1])]
    return out[:k], detail


_TITLE_AUTHOR = re.compile(r"\((?:comment|review comment on .+?) by ([A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?)\)\s*$")


def _who_and_when(db, sid, title, m):
    """Author (GitHub login), git author name (commits), created and updated times, and for comments and review comments the thread they belong to.
    Chunks written before a field was captured fall back: a comment's author from its title, a commit's `author` was the git name, a release's
    time from its tag date."""
    kind = m.get("kind") or "file"
    author, name = m.get("author"), m.get("author_name")
    if kind == "commit" and name is None:
        author, name = None, author                                      # written before the GitHub handle was captured
    if not author and kind in ("comment", "review"):
        author = (_TITLE_AUTHOR.search(title) or [None, None])[1]
    created = m.get("created") or (m.get("updated") if kind == "commit" else None) or (m.get("published") if kind == "release" else None)
    updated = m.get("updated") or (m.get("published") if kind == "release" else None)
    thread = None
    if kind in ("comment", "review") and m.get("number") is not None:
        r = db.execute("SELECT json_extract(meta, '$.author'), json_extract(meta, '$.created'), json_extract(meta, '$.kind') FROM chunks WHERE id = ?",
                       (f"{sid}:issue:{m['number']}",)).fetchone()
        if r:
            thread = {"number": m["number"], "kind": r[2], "author": r[0], "created": r[1]}
    return {"kind": kind, "author": author, "author_name": name, "created": created, "updated": updated, "thread": thread}


def hits(idx, q, text_chars=1200, **kw):
    """`search` as plain dicts: project, source, label and colour (from config), title, url, state, text (the chunk, cut), who and when (author handle,
    created and updated times, the thread of a comment), and the ranks and scores."""
    keys, detail = search(idx, q, **kw)
    out = []
    for pid, rid in keys:
        db = idx.stores[pid].db
        t, text, url, meta, sid, doc, state = db.execute(
            f"SELECT title, text, url, meta, source, doc, {STATE_SQL} FROM chunks c WHERE rowid=?", (rid,)).fetchone()
        m = json.loads(meta)
        src = idx.source(pid, sid)
        out.append({"project": pid, "source": sid, "key": src.key, "label": src.label, "color": src.color, "title": " ".join(t.split()), "url": url,
                    "doc": doc, "state": state, "labels": m.get("labels"), "text": text[:text_chars], "truncated": len(text) > text_chars,
                    **_who_and_when(db, sid, t, m), **detail.get((pid, rid), {})})
    return out


def show(rows, width=240):
    for n, h in enumerate(rows, 1):
        flag = f" [{h['state']}]" if h["state"] in ("closed", "merged") else ""
        snippet = " ".join(h["text"].split())[:width]
        print(f"{n}. [{h['key']}]{flag} {h['title']}\n   {h['url']}\n   {snippet}\n")


if __name__ == "__main__":
    a = sys.argv[1:]
    def opt(name, default=None, many=False):
        vals = []
        while name in a:
            i = a.index(name); vals.append(a[i + 1]); del a[i:i + 2]
        return vals if many else (vals[-1] if vals else default)
    k, uni = int(opt("-k", 8)), opt("--universe")
    projects, sources, kinds = opt("--project", many=True), opt("--source", many=True), opt("--kind", many=True)
    mode = "bm25" if "--bm25" in a else "vec" if "--vec" in a else "hybrid"
    q = " ".join(x for x in a if not x.startswith("--"))
    cfg = config.load()
    idx = Index(cfg, uni)
    if idx.missing:
        print(f"(not indexed yet: {', '.join(idx.missing)})", file=sys.stderr)
    emb = rr = None
    if mode != "bm25":
        try:
            from embed import load
            emb = load(cfg, "local" if "--local" in a else None)
        except Exception as e:
            print(f"(no embedder: {e}; keyword only)", file=sys.stderr)
    if "--rerank" in a:
        from rerank import Reranker
        rr = Reranker(cfg.search["reranker"]["model"])
    show(hits(idx, q, k=k, projects=projects, sources=sources, mode=mode, open_only="--open" in a, kinds=kinds, embedder=emb, reranker=rr,
              pool_docs=cfg.search["reranker"]["candidates"]))
