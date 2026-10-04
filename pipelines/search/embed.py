"""Embeddings: Qwen3-Embedding-0.6B on MPS (torch, transformers), last-token pooling, L2-normalised, stored as float16.
`fill()` embeds every chunk that has no vector for the current model or whose text changed since its vector was made;
`vector_search()` is exact brute force (a few 10k x 1024 matrix: milliseconds), so there is no ANN index to maintain.
usage: embed.py [--limit N]    (fill; resumable, commits per batch group)"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np, torch
from transformers import AutoModel, AutoTokenizer
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


def load():
    return Embedder()


def doc_text(title, text):
    return f"{title}\n\n{text}"


def fill(st, emb, limit=None, group=512, log=print):
    todo = st.db.execute("""SELECT c.rowid, c.title, c.text, c.hash FROM chunks c LEFT JOIN vec v ON v.rowid = c.rowid
                            WHERE v.rowid IS NULL OR v.hash != c.hash OR v.model != ? ORDER BY c.rowid""", (emb.name,)).fetchall()[:limit]
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
    """All vectors of `model` as one matrix, reloaded when the database file changes (the sync and embed passes are other processes)."""
    key = (model, st.path.stat().st_mtime_ns)
    if _cache.get("key") != key:
        rows = st.db.execute("SELECT rowid, v FROM vec WHERE model=?", (model,)).fetchall()
        _cache.update(key=key, ids=np.array([r[0] for r in rows]), m=np.vstack([np.frombuffer(r[1], dtype=np.float16) for r in rows]))
    return _cache["ids"], _cache["m"]


def vector_search(st, emb, q, k, source=None, open_only=False):
    ids, m = _matrix(st, emb.name)
    s = m @ emb.query(q).astype(np.float16)
    if source or open_only:
        ok = {r[0] for r in st.db.execute(
            f"SELECT rowid FROM chunks c WHERE (? IS NULL OR source=?) AND (? = 0 OR COALESCE({STATE_SQL}, 'open') NOT IN ('closed', 'merged'))",
            (source, source, int(open_only)))}
        s = np.where(np.isin(ids, list(ok)), s, -np.inf)
    top = np.argsort(-s)[:k]
    return [int(ids[i]) for i in top if np.isfinite(s[i])]


if __name__ == "__main__":
    st = Store()
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    t = time.time()
    emb = load()
    print(f"loaded {emb.name} on {emb.dev} in {time.time() - t:.0f}s")
    print(f"{fill(st, emb, limit)} chunks embedded")
