"""Caches in front of the two query-time models. A reranker score is a pure function of (model, instruction, query, document text), so repeated
queries, the canaries of `verify` and a page reload cost nothing the second time, and a changed document simply misses (its text is in the key).
Scores live in one SQLite file under the data directory; query embeddings, which are cheap to recompute and large to store, in a small LRU."""
import hashlib, sqlite3, time
from collections import OrderedDict


class CachedReranker:
    """Wraps anything with `.name` and `.scores(query, docs)`; `hits` and `misses` count the documents of the last call."""

    def __init__(self, inner, path, max_entries=200_000, salt=""):
        self.inner, self.max_entries, self.salt = inner, max_entries, salt
        self.name = inner.name
        self.hits = self.misses = 0
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.executescript("CREATE TABLE IF NOT EXISTS rerank(k TEXT PRIMARY KEY, score REAL, used REAL); CREATE INDEX IF NOT EXISTS rerank_used ON rerank(used);")

    def _key(self, query, doc):
        return hashlib.sha1(f"{self.name}\0{self.salt}\0{query}\0{doc}".encode()).hexdigest()

    def scores(self, query, docs):
        keys = [self._key(query, d) for d in docs]
        have = {}
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            have.update(self.db.execute(f"SELECT k, score FROM rerank WHERE k IN ({','.join('?' * len(part))})", part).fetchall())
        cached = [k for k in keys if k in have]
        todo = [i for i, k in enumerate(keys) if k not in have]
        self.hits, self.misses = len(docs) - len(todo), len(todo)
        if todo:
            fresh = self.inner.scores(query, [docs[i] for i in todo])
            now = time.time()
            self.db.executemany("INSERT OR REPLACE INTO rerank VALUES(?,?,?)", [(keys[i], s, now) for i, s in zip(todo, fresh)])
            have.update((keys[i], s) for i, s in zip(todo, fresh))
        if cached:
            self.db.executemany("UPDATE rerank SET used=? WHERE k=?", [(time.time(), k) for k in cached])
        self._prune()
        self.db.commit()
        return [have[k] for k in keys]

    def _prune(self):
        n = self.db.execute("SELECT count(*) FROM rerank").fetchone()[0]
        if n > self.max_entries:                          # drop the least recently used tenth beyond the limit
            self.db.execute("DELETE FROM rerank WHERE k IN (SELECT k FROM rerank ORDER BY used LIMIT ?)", (n - self.max_entries + self.max_entries // 10,))

    def size(self):
        return self.db.execute("SELECT count(*) FROM rerank").fetchone()[0]


class CachedEmbedder:
    """Wraps an embedder: `query(q)` is memoised in a small LRU; everything else (docs, name, dev, dim) is the inner embedder's."""

    def __init__(self, inner, size=512):
        self.inner, self.size, self.lru = inner, size, OrderedDict()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def query(self, q):
        if q in self.lru:
            self.lru.move_to_end(q)
            return self.lru[q]
        v = self.lru[q] = self.inner.query(q)
        if len(self.lru) > self.size:
            self.lru.popitem(last=False)
        return v
