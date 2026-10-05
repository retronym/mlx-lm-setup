"""Hybrid search over a universe of indexed projects as a small JSON server. Runs in .venv-jev (torch on MPS). Owns the embedder and the
reranker so they stay warm between queries; the indexes (one SQLite file per project, built by pipelines/search) are read-only here, and the
JSON config is re-read on every request, so adding a project or editing labels needs no restart.

  POST /search    {"query", "k"?, "universe"?, "projects"?: [id], "sources"?: [id | "project/source"], "mode"? hybrid|bm25|vec, "rerank"?, "open_only"?}
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

t0 = time.time()
cfg = config.load(a.config_dir)
emb, rr = embed.load(cfg), rerank.Reranker(cfg.search["reranker"]["model"])
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
    if not isinstance(q, str) or not q.strip():
        raise ValueError("query must be a non-empty string")
    mode, k = r.get("mode", "hybrid"), int(r.get("k", 8))
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k must be 1..{MAX_K}")
    c = config.load(a.config_dir)                     # cheap, and picks up edits to projects, labels and colours
    try:
        index = search.Index(c, r.get("universe"))
    except KeyError as e:
        raise ValueError(e.args[0])
    projects, sources = _strs(r.get("projects"), "projects"), _strs(r.get("sources"), "sources")
    for p in projects or []:
        if p not in index.universe.projects:
            raise ValueError(f"project {p!r} is not in universe {index.universe.id!r} (has: {', '.join(index.universe.projects)})")
    use_rr = bool(r.get("rerank", True))
    t = time.time()
    out = search.hits(index, q, k=k, projects=projects, sources=sources, mode=mode, open_only=bool(r.get("open_only")), embedder=emb,
                      reranker=rr if use_rr else None, pool_docs=c.search["reranker"]["candidates"])
    return {"query": q, "universe": index.universe.id, "mode": mode, "reranked": use_rr, "results": out, "missing": index.missing,
            "timing_ms": {"total": round((time.time() - t) * 1000)}}


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
