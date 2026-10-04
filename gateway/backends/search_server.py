"""Hybrid code/docs/issue search as a small JSON server. Runs in .venv-jev (torch on MPS). Owns the embedder and the reranker so
they stay warm between queries; the index itself (SQLite, built by pipelines/search/sync.py and embed.py) is read-only here.

  POST /search {"query", "k"?, "source"?, "mode"? hybrid|bm25|vec, "rerank"?, "open_only"?}  ->  {"results": [...], "timing_ms": {...}}
  POST /embed  {"input": str | [str], "kind"? "document" | "query"}                         ->  {"model", "dim", "embeddings": [[...]]}
  POST /rerank {"query", "documents": [str]}                                                ->  {"model", "scores": [P(relevant)]}
"""
import argparse, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--index-dir", required=True)        # pipelines/search: store.py, search.py, embed.py, rerank.py
ap.add_argument("--db", required=True)
ap.add_argument("--port", type=int, required=True)
a = ap.parse_args()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, a.index_dir)
from _http import serve                              # noqa: E402
from store import Store                              # noqa: E402
import embed, rerank, search                         # noqa: E402

t0 = time.time()
emb, rr = embed.Embedder(), rerank.Reranker()
st = Store(a.db)
n_vec = st.db.execute("SELECT count(*) FROM vec").fetchone()[0]
if n_vec:
    embed.vector_search(st, emb, "warm up", 1)       # loads the vector matrix; also compiles the embedder's kernels
rr.scores("warm up", ["warm up"])
print(f"search ready in {time.time() - t0:.1f}s: {n_vec} vectors, embedder {emb.name}, reranker {rerank.MODEL} on {emb.dev}", flush=True)
MODES = {"hybrid", "bm25", "vec"}
MAX_K, MAX_BATCH = 50, 64


def _search(r):
    q = r["query"]
    if not isinstance(q, str) or not q.strip():
        raise ValueError("query must be a non-empty string")
    mode, k = r.get("mode", "hybrid"), int(r.get("k", 8))
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k must be 1..{MAX_K}")
    source = r.get("source") or None
    use_rr = bool(r.get("rerank", True))
    store = Store(a.db)                               # a connection per request: the server handles requests on different threads
    t = time.time()
    out = search.hits(store, q, k=k, source=source, mode=mode, open_only=bool(r.get("open_only")), embedder=emb,
                      reranker=rr if use_rr else None)
    return {"query": q, "mode": mode, "reranked": use_rr, "results": out, "timing_ms": {"total": round((time.time() - t) * 1000)}}


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
    return {"model": rerank.MODEL, "scores": rr.scores(r["query"], docs)}


serve(a.port, {"model": "scala-search", "embedder": emb.name, "reranker": rerank.MODEL, "vectors": n_vec, "device": emb.dev},
      {"/search": _search, "/embed": _embed, "/rerank": _rerank})
