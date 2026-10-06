"""Hybrid search over a universe of indexed projects as a small JSON server. Runs in .venv-jev (torch on MPS). Owns the embedder and the
reranker so they stay warm between queries; the indexes (one SQLite file per project, built by pipelines/search) are read-only here, and the
JSON config is re-read on every request, so adding a project or editing labels needs no restart.

  POST /search    {"query", "k"?, "universe"?, "projects"?: [id], "sources"?: [id | "project/source"], "kinds"?: [kind], "mode"? hybrid|bm25|vec, "rerank"?, "open_only"?, "explain"?,
                   "linked_to"? (a hit ref, `scala/bug#123`, `#123`, a sha: [..] too), "link_type"? [relation | edge type], "has_link"? [relation | no_relation], "refs_in_query"?, "related"? (false, or how many; default from search.json `related`), "link_boost"?,
                   "since"?, "until"? (YYYY[-MM[-DD]]), "date"? created|updated, "authors"? [GitHub login or git author name], "sort"? relevance|recent}
                  (an empty query lists what the filters match, newest first)
                  -> {"universe", "results": [...], "timing_ms": {...}, "missing": [projects not indexed yet]}
  POST /embed     {"input": str | [str], "kind"? "document" | "query"}  ->  {"model", "dim", "embeddings": [[...]]}
  POST /rerank    {"query", "documents": [str]}                         ->  {"model", "scores": [P(relevant)]}
"""
import argparse, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--index-dir", required=True)        # pipelines/search: config.py, store.py, search.py, embed.py, rerank.py
ap.add_argument("--config-dir")                      # default <index-dir>/config
ap.add_argument("--port", type=int, required=True)
a = ap.parse_args()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, a.index_dir)
from _http import serve                              # noqa: E402
import config, embed, rerank, search                 # noqa: E402
from store import check_where                        # noqa: E402

t0 = time.time()
cfg = config.load(a.config_dir)
emb, rr = embed.load(cfg, "local"), rerank.Reranker(cfg.search["reranker"]["model"])
if cfg.search["cache"]["enabled"]:                   # repeated queries (canaries, reloads, refinements) cost no model call
    from cache import CachedEmbedder, CachedReranker
    emb = CachedEmbedder(emb)
    rr = CachedReranker(rr, cfg.data_path("cache.db"), cfg.search["cache"]["max_entries"], salt=rerank.TASK)
idx = search.Index(cfg)
n_vec = sum(s.db.execute("SELECT count(*) FROM vec").fetchone()[0] for s in idx.stores.values())
if n_vec:
    from embed import vector_search
    q = emb.query("warm up")
    for s in idx.stores.values():
        vector_search(s, q, emb.name, 1)             # loads each vector matrix; also compiles the embedder's kernels
rr.scores("warm up", ["warm up"])
print(f"search ready in {time.time() - t0:.1f}s: {len(idx.stores)} projects, {n_vec} vectors, embedder {emb.name}, reranker {rr.name} on {emb.dev}", flush=True)
MODES = {"hybrid", "bm25", "vec"}
MAX_K, MAX_BATCH = 50, 64


def _strs(v, name):
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ValueError(f"{name} must be a list of strings")
    return v or None


def _search(r):
    q = r["query"]
    if not isinstance(q, str):
        raise ValueError("query must be a string (empty: list what the filters match, newest first)")
    mode, k = r.get("mode", "hybrid"), int(r.get("k", 20))
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k must be 1..{MAX_K}")
    c = config.load(a.config_dir)                     # cheap, and picks up edits to projects, labels and colours
    try:
        index = search.Index(c, r.get("universe"))
    except KeyError as e:
        raise ValueError(e.args[0])
    try:
        return _search_in(index, r, q, k, mode, c)
    finally:
        index.close()                                 # one Index per request: leave no connection to the garbage collector (the fd limit is low under launchd)


def _search_in(index, r, q, k, mode, c):
    projects, sources, kinds = _strs(r.get("projects"), "projects"), _strs(r.get("sources"), "sources"), _strs(r.get("kinds"), "kinds")
    for kd in kinds or []:
        if kd not in config.KINDS:
            raise ValueError(f"kind {kd!r} is not one of {', '.join(config.KINDS)}")
    for p in projects or []:
        if p not in index.universe.projects:
            raise ValueError(f"project {p!r} is not in universe {index.universe.id!r} (has: {', '.join(index.universe.projects)})")
    use_rr = bool(r.get("rerank", True)) and bool(q.strip())
    sort = r.get("sort", "relevance")
    if sort not in ("relevance", "recent"):
        raise ValueError("sort must be relevance or recent")
    text_chars = int(r.get("text_chars", 1200))
    if not 1 <= text_chars <= 50000:
        raise ValueError("text_chars must be 1..50000")
    try:
        lf, linfo = search.link_filter(index, r.get("linked_to"), _strs(r.get("link_type"), "link_type"), _strs(r.get("has_link"), "has_link"))
    except ValueError as e:
        raise ValueError(str(e))
    where = check_where({key: r.get(key) for key in ("since", "until", "date", "authors")}, c.search["me"])
    t = time.time()
    out = search.hits(index, q, k=k, projects=projects, sources=sources, mode=mode, open_only=bool(r.get("open_only")), kinds=kinds, embedder=emb,
                      reranker=rr if use_rr else None, explain=bool(r.get("explain")), text_chars=text_chars, link_filter=lf,
                      refs_in_query=bool(r.get("refs_in_query", True)), link_boost=r.get("link_boost"), where=where, sort=sort, **search.tuning(c))
    rel_k = r.get("related")
    related = [] if lf or rel_k is False or not q.strip() else search.related(index, out, None if rel_k in (None, True) else int(rel_k), kinds, projects, bool(r.get("open_only")), explain=bool(r.get("explain")), where=where)
    cached = {"rerank_hits": rr.hits, "rerank_misses": rr.misses} if use_rr and hasattr(rr, "hits") else None
    return {"query": q, "universe": index.universe.id, "mode": mode, "reranked": use_rr, "results": out, "missing": index.missing, "links_available": index.links is not None, "related": related, **({"link_filter": linfo} if lf else {}), **({"where": where} if where else {}),
            "timing_ms": {"total": round((time.time() - t) * 1000)}, **({"cache": cached} if cached else {})}


def _embed(r):
    inp = r["input"]
    texts = [inp] if isinstance(inp, str) else inp
    if not isinstance(texts, list) or not texts or not all(isinstance(x, str) for x in texts) or len(texts) > MAX_BATCH:
        raise ValueError(f"input must be a string or a list of 1..{MAX_BATCH} strings")
    kind = r.get("kind", "document")
    if kind not in ("document", "query"):
        raise ValueError("kind must be document or query")
    vecs = emb.docs(texts) if kind == "document" else [emb.query(x) for x in texts]
    return {"model": emb.name, "dim": int(len(vecs[0])), "embeddings": [[round(float(x), 6) for x in v] for v in vecs]}


def _rerank(r):
    docs = r["documents"]
    if not isinstance(r["query"], str) or not isinstance(docs, list) or not docs or not all(isinstance(d, str) for d in docs) or len(docs) > MAX_BATCH:
        raise ValueError(f"query must be a string and documents a list of 1..{MAX_BATCH} strings")
    return {"model": rr.name, "scores": rr.scores(r["query"], docs)}


serve(a.port, {"model": "scala-search", "embedder": emb.name, "reranker": rr.name, "vectors": n_vec, "device": emb.dev},
      {"/search": _search, "/embed": _embed, "/rerank": _rerank})
