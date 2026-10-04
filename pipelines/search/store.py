"""SQLite store: chunks (text, identity, content hash), an FTS5 keyword index, and vectors, in one file.

Sync is text-level and idempotent: a source yields the chunks it currently has for a scope, `apply()` diffs them against the
stored rows by content hash (insert / update / delete / skip). Embedding is a separate pass that fills whatever has no vector
for the current model, so keyword search works before any model exists and a model change is just a refill.
"""
import hashlib, json, re, sqlite3, time
from pathlib import Path

DB = Path(__file__).parent / "data" / "search.db"
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|_")

# An issue is "closed" by its own state; a comment takes the state of its issue (looked up at query time, so closing an issue
# needs no rewrite of its comments). `c` is the chunks row.
STATE_SQL = ("COALESCE(json_extract(c.meta, '$.state'), (SELECT json_extract(i.meta, '$.state') FROM chunks i "
             "WHERE i.id = c.source || ':issue:' || json_extract(c.meta, '$.number')))")


def fts_text(s):
    """Index text plus its camelCase / snake_case parts, so `typedApply` is found by `typed apply`."""
    parts = {p.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", s) for p in _CAMEL.split(w) if len(p) > 1}
    return s + "\n" + " ".join(sorted(parts))


def sha(s):
    return hashlib.sha1(s.encode()).hexdigest()


class Chunk:
    __slots__ = ("id", "doc", "title", "text", "url", "meta")

    def __init__(self, id, doc, title, text, url, meta=None):
        self.id, self.doc, self.title, self.text, self.url, self.meta = id, doc, title, text, url, meta or {}

    @property
    def hash(self):
        return sha(self.title + "\0" + self.text)


class Store:
    def __init__(self, path=DB):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chunks(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, source TEXT, doc TEXT, title TEXT,
                text TEXT, hash TEXT, url TEXT, meta TEXT, updated REAL);
            CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(source, doc);
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, body, tokenize="unicode61 remove_diacritics 2");
            CREATE TABLE IF NOT EXISTS vec(rowid INTEGER PRIMARY KEY, model TEXT, hash TEXT, v BLOB);
            CREATE TABLE IF NOT EXISTS state(source TEXT, k TEXT, v TEXT, PRIMARY KEY(source, k));
        """)

    # --- per-source cursors: last indexed commit, last updated_at, per-file blob sha ---
    def get(self, source, k, default=None):
        r = self.db.execute("SELECT v FROM state WHERE source=? AND k=?", (source, k)).fetchone()
        return r[0] if r else default

    def put(self, source, k, v):
        self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?,?)", (source, k, v))

    def commit(self):
        self.db.commit()

    # --- the diff ---
    def apply(self, source, chunks, doc=None, delete_missing_docs=None):
        """Upsert `chunks`. With `doc`, the chunks are the complete current content of that one document, so stored chunks of
        it that are not in `chunks` are deleted. Returns (added, changed, deleted, unchanged)."""
        have = {}
        if doc is not None:
            have = {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT id, hash, rowid FROM chunks WHERE source=? AND doc=?", (source, doc))}
        a = c = u = 0
        for ch in chunks:
            row = self.db.execute("SELECT rowid, hash, meta, url FROM chunks WHERE id=?", (ch.id,)).fetchone()
            have.pop(ch.id, None)
            if row and row[1] == ch.hash:
                if (row[2], row[3]) != (json.dumps(ch.meta), ch.url):      # metadata only (issue closed, line moved): no re-embed
                    self.db.execute("UPDATE chunks SET meta=?, url=? WHERE rowid=?", (json.dumps(ch.meta), ch.url, row[0]))
                u += 1
                continue
            vals = (ch.id, source, ch.doc, ch.title, ch.text, ch.hash, ch.url, json.dumps(ch.meta), time.time())
            if row:
                self.db.execute("UPDATE chunks SET id=?, source=?, doc=?, title=?, text=?, hash=?, url=?, meta=?, updated=? WHERE rowid=?", (*vals, row[0]))
                self.db.execute("DELETE FROM fts WHERE rowid=?", (row[0],))
                rid, c = row[0], c + 1
            else:
                rid = self.db.execute("INSERT INTO chunks(id, source, doc, title, text, hash, url, meta, updated) VALUES(?,?,?,?,?,?,?,?,?)", vals).lastrowid
                a += 1
            self.db.execute("INSERT INTO fts(rowid, title, body) VALUES(?,?,?)", (rid, fts_text(ch.title), fts_text(ch.text)))
        d = 0
        for cid in have:
            d += self.delete_chunk(cid)
        return a, c, d, u

    def delete_chunk(self, cid):
        row = self.db.execute("SELECT rowid FROM chunks WHERE id=?", (cid,)).fetchone()
        if not row:
            return 0
        for t in ("chunks", "fts", "vec"):
            self.db.execute(f"DELETE FROM {t} WHERE rowid=?", (row[0],))
        return 1

    def delete_doc(self, source, doc):
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE source=? AND doc=?", (source, doc))]
        return sum(self.delete_chunk(i) for i in ids)

    def stats(self):
        return self.db.execute("""SELECT c.source, count(*), count(v.rowid) FROM chunks c
            LEFT JOIN vec v ON v.rowid=c.rowid AND v.hash=c.hash GROUP BY c.source""").fetchall()
