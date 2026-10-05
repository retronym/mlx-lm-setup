"""Embeddings: Qwen3-Embedding-0.6B on MPS (torch, transformers), last-token pooling, L2-normalised, stored as float16.
`fill()` embeds every chunk that has no vector for the current model or whose text changed since its vector was made;
`vector_search()` is exact brute force (a few 10k x 1024 matrix: milliseconds), so there is no ANN index to maintain.
usage: embed.py [universe | project | project/source ...] [--max-priority N] [--limit N]   (fills in priority order; resumable)"""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np, torch
from transformers import AutoModel, AutoTokenizer
import config, runstate
from store import Store, STATE_SQL

MODEL = os.environ.get("EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
TASK = "Given a question or code snippet about the Scala compiler, standard library or build tools, retrieve the most relevant source code, documentation or issue text"
MAXLEN = 768


class Embedder:
    def __init__(self, model=MODEL):
        self.name = model
        self.tok = AutoTokenizer.from_pretrained(model, padding_side="left")
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
        self.dev = dev
        self.m = AutoModel.from_pretrained(model, torch_dtype=torch.float16 if dev == "mps" else torch.float32).to(dev).eval()

    @torch.no_grad()
    def _enc(self, texts):
        b = self.tok(texts, padding=True, truncation=True, max_length=MAXLEN, return_tensors="pt").to(self.dev)
        h = self.m(**b).last_hidden_state[:, -1]          # left padding: the last position is the last real token
        return torch.nn.functional.normalize(h.float(), dim=-1).cpu().numpy()

    def docs(self, texts, batch=16):
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))        # length-sorted batches waste less padding
        out = np.zeros((len(texts), self.m.config.hidden_size), dtype=np.float32)
        for s in range(0, len(order), batch):
            idx = order[s:s + batch]
            out[idx] = self._enc([texts[i] for i in idx])
        return out

    def query(self, q):
        return self._enc([f"Instruct: {TASK}\nQuery: {q}"])[0]


def load(cfg=None):
    return Embedder((cfg.search["embedder"]["model"] if cfg else MODEL))


def doc_text(title, text):
    return f"{title}\n\n{text}"


def fill(st, emb, source=None, limit=None, group=512, log=print):
    """Embed the chunks of `source` (all when None) that have no vector for `emb`'s model or whose text changed since."""
    todo = st.db.execute("""SELECT c.rowid, c.title, c.text, c.hash FROM chunks c LEFT JOIN vec v ON v.rowid = c.rowid
                            WHERE (? IS NULL OR c.source = ?) AND (v.rowid IS NULL OR v.hash != c.hash OR v.model != ?)
                            ORDER BY c.updated DESC""", (source, source, emb.name)).fetchall()[:limit]      # newest first
    t0 = time.time()
    for s in range(0, len(todo), group):
        rows = todo[s:s + group]
        vecs = emb.docs([doc_text(r[1], r[2]) for r in rows])
        st.db.executemany("INSERT OR REPLACE INTO vec VALUES(?,?,?,?)",
                          [(r[0], emb.name, r[3], vecs[i].astype(np.float16).tobytes()) for i, r in enumerate(rows)])
        st.commit()
        done = s + len(rows)
        log(f"  embedded {done}/{len(todo)}  ({done / (time.time() - t0):.1f} chunks/s)")
    return len(todo)


_cache = {}


def _matrix(st, model):
    """All vectors of `model` in one store as a matrix, reloaded when the database file changes (sync and embed are other processes)."""
    key = (str(st.path), model)
    stamp = st.path.stat().st_mtime_ns
    c = _cache.get(key)
    if c is None or c["stamp"] != stamp:
        rows = st.db.execute("SELECT rowid, v FROM vec WHERE model=?", (model,)).fetchall()
        c = _cache[key] = {"stamp": stamp, "ids": np.array([r[0] for r in rows], dtype=np.int64),
                           "m": np.vstack([np.frombuffer(r[1], dtype=np.float16) for r in rows]) if rows else np.zeros((0, 1), dtype=np.float16)}
    return c["ids"], c["m"]


def vector_search(st, qvec, model, k, sources=None, open_only=False):
    """[(rowid, cosine)] best first. `qvec` is the already-embedded query, so one query serves every store of a universe."""
    ids, m = _matrix(st, model)
    if not len(ids):
        return []
    s = m @ qvec.astype(np.float16)
    if sources or open_only:
        marks = ",".join("?" * len(sources)) if sources else ""
        ok = {r[0] for r in st.db.execute(
            f"SELECT rowid FROM chunks c WHERE ({'1' if not sources else 'c.source IN (' + marks + ')'}) AND (? = 0 OR COALESCE({STATE_SQL}, 'open') NOT IN ('closed', 'merged'))",
            (*(sources or ()), int(open_only)))}
        s = np.where(np.isin(ids, list(ok)), s, -np.inf)
    top = np.argsort(-s)[:k]
    return [(int(ids[i]), float(s[i])) for i in top if np.isfinite(s[i])]


def targets(cfg, args, max_priority=9):
    """Sources named by CLI args (universe, project or project/source; default: the default universe), enabled, within the priority limit,
    sorted by priority then project."""
    names = args or [cfg.default_universe().id]
    out = {}
    for n in names:
        if n in cfg.universes:
            srcs = cfg.universe_sources(n)
        elif n in cfg.projects:
            srcs = cfg.projects[n].sources
        elif "/" in n and n.split("/")[0] in cfg.projects:
            srcs = [s for s in cfg.projects[n.split("/")[0]].sources if s.id == n.split("/")[1]]
        else:
            raise SystemExit(f"unknown universe, project or source {n!r}")
        for s in srcs:
            out[s.key] = s
    return sorted((s for s in out.values() if s.enabled and s.priority <= max_priority), key=lambda s: (s.priority, s.project, s.id))


if __name__ == "__main__":
    a = sys.argv[1:]
    def opt(name, default=None):
        if name in a:
            i = a.index(name); v = a[i + 1]; del a[i:i + 2]; return v
        return default
    limit, maxp = opt("--limit"), int(opt("--max-priority", 9))
    cfg = config.load()
    srcs = targets(cfg, a, maxp)
    run = runstate.Run(cfg, "embed", [s.key for s in srcs])
    t = time.time()
    run.s["phase"] = "loading the embedding model"; run._write(force=True)
    emb = load(cfg)
    run.log(f"loaded {emb.name} on {emb.dev} in {time.time() - t:.0f}s")
    for s in srcs:
        if not cfg.project_db(s.project).exists():
            continue
        run.source(s.key, "embed")
        n = fill(Store(cfg.project_db(s.project)), emb, source=s.id, limit=int(limit) if limit else None, log=lambda m, k=s.key: run.log(f"{k}: {m.strip()}"))
        if n:
            run.log(f"{s.key}: {n} chunks embedded")
    run.finish()
