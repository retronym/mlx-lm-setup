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

# What a chunk is (config.KINDS): the `kind` its source wrote, or "file" for a chunk of a file in a git tree, which carries none.
KIND_SQL = "COALESCE(json_extract(c.meta, '$.kind'), 'file')"


LINKED_SQL = "EXISTS (SELECT 1 FROM temp.linkfilter f WHERE f.source = c.source AND f.doc = c.doc)"

# When a chunk was written or last changed: issues, PRs, comments, reviews and commits carry `created` / `updated`; a tag only its `published` date.
# A file in a git tree has neither, so a date filter leaves files out.
DATE_SQL = {"created": "COALESCE(json_extract(c.meta, '$.created'), json_extract(c.meta, '$.published'))",
            "updated": "COALESCE(json_extract(c.meta, '$.updated'), json_extract(c.meta, '$.published'), json_extract(c.meta, '$.created'))"}
# Expression indexes for the date and author filters (`chunk_filter` writes exactly these expressions, so SQLite uses them): listing "everything by me
# this year" reads a few hundred rows instead of every chunk's JSON. Built on the first open of a database that lacks them (a few seconds).
INDEXES = "".join(f"CREATE INDEX IF NOT EXISTS {name} ON chunks({expr});\n" for name, expr in (
    ("chunks_author", "lower(json_extract(meta, '$.author'))"), ("chunks_author_name", "lower(json_extract(meta, '$.author_name'))"),
    ("chunks_created", DATE_SQL["created"].replace("c.meta", "meta")), ("chunks_updated", DATE_SQL["updated"].replace("c.meta", "meta"))))
_DAY = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def check_where(where, me=()):
    """`where` normalised, or ValueError: {"since", "until"} (`YYYY[-MM[-DD]]`, both ends inclusive at their own precision: until 2024 is the end of 2024),
    "date" (`created`, the default, or `updated`), "authors" ([GitHub login or git author name], any of them, case-insensitive; "me" is `me`, search.json's `me`). None when nothing is set."""
    if not where:
        return None
    unknown = set(where) - {"since", "until", "date", "authors"}
    if unknown:
        raise ValueError(f"unknown filter keys {sorted(unknown)}")
    out = {k: v for k, v in where.items() if v not in (None, "", [])}
    for k in ("since", "until"):
        if k in out and not (isinstance(out[k], str) and _DAY.match(out[k])):
            raise ValueError(f"{k} must be YYYY, YYYY-MM or YYYY-MM-DD, not {out[k]!r}")
    if out.get("date", "created") not in DATE_SQL:
        raise ValueError(f"date must be one of {sorted(DATE_SQL)}")
    a = out.get("authors")
    if a is not None:
        a = [a] if isinstance(a, str) else a
        if not isinstance(a, list) or not all(isinstance(x, str) and x.strip() for x in a):
            raise ValueError("authors must be a list of names")
        out["authors"] = list(dict.fromkeys(y.lower() for x in a for y in (me if x.strip().lower() == "me" and me else [x.strip().lstrip("@")])))
    return out if set(out) - {"date"} else None


def chunk_filter(sources=None, open_only=False, kinds=None, linked=None, where=None):
    """An ` AND ...` condition on the chunks row `c` and its arguments: source ids, kinds, not closed or merged, `linked`: "in" (the document is in the
    connection's temp `linkfilter` table, see `Store.set_link_filter`) or "out" (it is not), and `where` (`check_where`): a date range and authors.
    An author is matched against the GitHub login and, for commits (which mostly carry only a git name), the git author name."""
    cond, args = "", []
    if where:
        d = DATE_SQL[where.get("date", "created")]
        if where.get("since"):                                   # a range on the indexed expression: "2026" <= "2026-03-01T..."
            cond += f" AND {d} >= ?"; args.append(where["since"])
        if where.get("until"):                                   # inclusive at its precision: "2024-05~" sorts after every date in May 2024
            cond += f" AND {d} <= ?"; args.append(where["until"] + "~")
        if where.get("authors"):
            ph = ",".join("?" * len(where["authors"]))
            cond += (f" AND (lower(json_extract(c.meta, '$.author')) IN ({ph}) OR lower(json_extract(c.meta, '$.author_name')) IN ({ph}))")
            args += where["authors"] * 2
    if linked:
        cond += f" AND {'NOT ' if linked == 'out' else ''}{LINKED_SQL}"
    if sources:
        cond += f" AND c.source IN ({','.join('?' * len(sources))})"; args += list(sources)
    if kinds:
        cond += f" AND {KIND_SQL} IN ({','.join('?' * len(kinds))})"; args += list(kinds)
    if open_only:
        cond += f" AND COALESCE({STATE_SQL}, 'open') NOT IN ('closed', 'merged')"
    return cond, args


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
        self.db = sqlite3.connect(path, timeout=60)         # a sync and an embed pass may run at the same time
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chunks(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, source TEXT, doc TEXT, title TEXT,
                text TEXT, hash TEXT, url TEXT, meta TEXT, updated REAL);
            CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(source, doc);
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, body, tokenize="unicode61 remove_diacritics 2");
            CREATE TABLE IF NOT EXISTS vec(rowid INTEGER PRIMARY KEY, model TEXT, hash TEXT, v BLOB);
            CREATE TABLE IF NOT EXISTS state(source TEXT, k TEXT, v TEXT, PRIMARY KEY(source, k));
        """ + INDEXES)

    # --- per-source cursors: last indexed commit, last updated_at, per-file blob sha ---
    def get(self, source, k, default=None):
        r = self.db.execute("SELECT v FROM state WHERE source=? AND k=?", (source, k)).fetchone()
        return r[0] if r else default

    def put(self, source, k, v):
        self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?,?)", (source, k, v))

    def commit(self):
        self.db.commit()

    def set_link_filter(self, pairs):
        """The documents `chunk_filter(linked=...)` tests against, as (source id, doc) pairs, in a temp table of this connection (so a read-only
        database is fine and two requests never see each other's)."""
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS linkfilter(source TEXT, doc TEXT, PRIMARY KEY(source, doc)) WITHOUT ROWID")
        self.db.execute("DELETE FROM temp.linkfilter")
        self.db.executemany("INSERT OR IGNORE INTO temp.linkfilter VALUES(?,?)", pairs)

    # --- the diff ---
    def apply(self, source, chunks, doc=None, delete_missing_docs=None, existing_only=False):
        """Upsert `chunks`. With `doc`, the chunks are the complete current content of that one document, so stored chunks of
        it that are not in `chunks` are deleted. With `existing_only`, chunks that are not stored yet are skipped (a metadata repair never ingests
        anything new). Returns (added, changed, deleted, unchanged)."""
        have = {}
        if doc is not None:
            have = {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT id, hash, rowid FROM chunks WHERE source=? AND doc=?", (source, doc))}
        a = c = u = 0
        for ch in chunks:
            row = self.db.execute("SELECT rowid, hash, meta, url FROM chunks WHERE id=?", (ch.id,)).fetchone()
            have.pop(ch.id, None)
            if existing_only and not row:
                continue
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
