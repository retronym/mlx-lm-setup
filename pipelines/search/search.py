#!/usr/bin/env python3
"""Hybrid search: FTS5 BM25 + vector cosine (if vectors exist), fused with reciprocal rank fusion.
usage: search.py [-k 8] [--source scalac|scala3docs|bug] [--open] [--bm25|--vec] [--rerank] <query>"""
import json, re, sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from store import Store, _CAMEL, STATE_SQL

STOP = set("a an the of in on to is are was be for and or not with how what where why does do this that it as by from at".split())


def fts_query(q):
    """OR of the query's words and their camelCase parts, title matches weighted by the bm25 column weights below."""
    terms = []
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", q):
        for t in [w] + (_CAMEL.split(w) if len(w) > 3 else []):
            t = t.lower()
            if len(t) > 1 and t not in STOP and t not in terms:
                terms.append(t)
    return " OR ".join(f'"{t}"' for t in terms)


def bm25(st, q, k, source=None, open_only=False):
    fq = fts_query(q)
    if not fq:
        return []
    sql = """SELECT c.rowid, bm25(fts, 3.0, 1.0) s FROM fts JOIN chunks c ON c.rowid = fts.rowid
             WHERE fts MATCH ? {} ORDER BY s LIMIT ?"""
    cond = ""
    args = [fq]
    if source:
        cond += " AND c.source = ?"; args.append(source)
    if open_only:
        cond += f" AND COALESCE({STATE_SQL}, 'open') NOT IN ('closed', 'merged')"
    return [r[0] for r in st.db.execute(sql.format(cond), (*args, k))]


def fuse(rankings, k=60):
    score = {}
    for r in rankings:
        for i, rid in enumerate(r):
            score[rid] = score.get(rid, 0) + 1 / (k + i + 1)
    return sorted(score, key=score.get, reverse=True)


def search(st, q, k=8, source=None, mode="hybrid", open_only=False, embedder=None, reranker=None, pool_docs=30):
    """Ranked chunk rowids plus per-hit details: {rowid: {"bm25": rank, "vec": rank, "rerank": score}} (ranks are 1-based, in the
    candidate pools of the two retrievers)."""
    pool = max(50, k * 5)
    rankings, names = [], []
    if mode in ("hybrid", "bm25"):
        rankings.append(bm25(st, q, pool, source, open_only)), names.append("bm25")
    if mode in ("hybrid", "vec") and embedder is not None:
        from embed import vector_search
        rankings.append(vector_search(st, embedder, q, pool, source, open_only)), names.append("vec")
    detail = {}
    for name, r in zip(names, rankings):
        for i, rid in enumerate(r):
            detail.setdefault(rid, {})[name] = i + 1
    seen, out = set(), []                      # one hit per document: the best chunk of an issue, file or page
    for rid in fuse(rankings):
        key = st.db.execute("SELECT source, doc FROM chunks WHERE rowid=?", (rid,)).fetchone()
        if key not in seen:
            seen.add(key)
            out.append(rid)
    if reranker is not None and out:
        cand = out[:pool_docs]
        docs = [" ".join(st.db.execute("SELECT title, text FROM chunks WHERE rowid=?", (r,)).fetchone()) for r in cand]
        sc = reranker.scores(q, docs)
        for r, x in zip(cand, sc):
            detail[r]["rerank"] = round(x, 4)
        out = [r for r, _ in sorted(zip(cand, sc), key=lambda x: -x[1])]
    return out[:k], detail


def hits(st, q, text_chars=1200, **kw):
    """`search` as plain dicts: source, title, url, state (open/closed for issues), text (the chunk, cut), and the ranks/scores."""
    rids, detail = search(st, q, **kw)
    out = []
    for rid in rids:
        t, text, url, meta, src, doc, state = st.db.execute(f"SELECT title, text, url, meta, source, doc, {STATE_SQL} FROM chunks c WHERE rowid=?", (rid,)).fetchone()
        m = json.loads(meta)
        out.append({"source": src, "title": " ".join(t.split()), "url": url, "doc": doc, "state": state, "labels": m.get("labels"),
                    "text": text[:text_chars], "truncated": len(text) > text_chars, **detail.get(rid, {})})
    return out


def show(st, rids, width=240):
    for n, rid in enumerate(rids, 1):
        t, text, url, src, state = st.db.execute(f"SELECT title, text, url, source, {STATE_SQL} FROM chunks c WHERE rowid=?", (rid,)).fetchone()
        flag = " [closed]" if state == "closed" else ""
        snippet = " ".join(text.split())[:width]
        print(f"{n}. [{src}]{flag} {t}\n   {url}\n   {snippet}\n")


if __name__ == "__main__":
    a = sys.argv[1:]
    def opt(name, default=None):
        if name in a:
            i = a.index(name); v = a[i + 1]; del a[i:i + 2]; return v
        return default
    k, source = int(opt("-k", 8)), opt("--source")
    mode = "bm25" if "--bm25" in a else "vec" if "--vec" in a else "hybrid"
    open_only = "--open" in a
    q = " ".join(x for x in a if not x.startswith("--"))
    st = Store()
    emb = None
    if mode != "bm25":
        try:
            from embed import load
            emb = load()
        except Exception as e:
            print(f"(no embedder: {e}; keyword only)", file=sys.stderr)
    rr = None
    if "--rerank" in a:
        from rerank import Reranker
        rr = Reranker()
    show(st, search(st, q, k, source, mode, open_only, emb, rr)[0])
