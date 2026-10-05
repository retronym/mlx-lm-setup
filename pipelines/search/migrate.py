#!/usr/bin/env python3
"""One-off: split the legacy single index (data/search.db, sources scalac / bug / scalapr / scala3 / scala3docs) into one database per
project (data/projects/<id>/index.db) without re-embedding: chunks, full-text index, vectors and sync state are copied, chunk ids
are re-prefixed with the new source id. The old file is opened read-only and never modified.

usage: python migrate.py [--old PATH] [--force]"""
import argparse, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import config
from store import Store, fts_text

LEGACY = {"scalac": ("scala2", "code"), "bug": ("scala2", "issues"), "scalapr": ("scala2", "prs"),
          "scala3": ("scala3", "code"), "scala3docs": ("scala3", "docs")}
STATE_KEYS = ("file:%", "head", "since_issues", "since_comments", "since_reviews")


def migrate(cfg, old_path, force=False, log=print):
    old = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)
    legacy_counts = {s: (n, v) for s, n, v in old.execute(
        "SELECT c.source, count(*), count(v.rowid) FROM chunks c LEFT JOIN vec v ON v.rowid = c.rowid AND v.hash = c.hash GROUP BY c.source")}
    unknown = set(legacy_counts) - set(LEGACY)
    if unknown:
        raise SystemExit(f"legacy sources without a mapping: {sorted(unknown)}")
    for src, (pid, sid) in LEGACY.items():
        if src in legacy_counts and (pid not in cfg.projects or sid not in {s.id for s in cfg.projects[pid].sources}):
            raise SystemExit(f"config has no source {pid}/{sid} (needed for legacy source {src!r})")
    stores = {}
    for pid in sorted({LEGACY[s][0] for s in legacy_counts}):
        db = cfg.project_db(pid)
        if db.exists() and db.stat().st_size > 0:
            if not force:
                raise SystemExit(f"{db} exists; pass --force to replace it")
            db.unlink()
        stores[pid] = Store(db)
    for src, (n_chunks, n_vec) in legacy_counts.items():
        pid, sid = LEGACY[src]
        st, t0 = stores[pid], time.time()
        rows = old.execute("""SELECT c.rowid, c.id, c.doc, c.title, c.text, c.hash, c.url, c.meta, c.updated, v.model, v.hash, v.v
                              FROM chunks c LEFT JOIN vec v ON v.rowid = c.rowid WHERE c.source = ?""", (src,))
        for i, (rid, cid, doc, title, text, h, url, meta, upd, vmodel, vhash, vblob) in enumerate(rows, 1):
            new = st.db.execute("INSERT INTO chunks(id, source, doc, title, text, hash, url, meta, updated) VALUES(?,?,?,?,?,?,?,?,?)",
                                (f"{sid}:{cid.split(':', 1)[1]}", sid, doc, title, text, h, url, meta, upd)).lastrowid
            st.db.execute("INSERT INTO fts(rowid, title, body) VALUES(?,?,?)", (new, fts_text(title), fts_text(text)))
            if vblob is not None:
                st.db.execute("INSERT INTO vec VALUES(?,?,?,?)", (new, vmodel, vhash, vblob))
            if i % 20000 == 0:
                st.commit(); log(f"  {src} -> {pid}/{sid}: {i}/{n_chunks}")
        for pat in STATE_KEYS:
            for k, v in old.execute("SELECT k, v FROM state WHERE source = ? AND k LIKE ?", (src, pat)):
                st.put(sid, k, v)
        st.commit()
        log(f"{src:10} -> {pid}/{sid:7} {n_chunks:>7} chunks, {n_vec:>7} vectors  ({time.time() - t0:.0f} s)")
    # verify
    for src, (n_chunks, n_vec) in legacy_counts.items():
        pid, sid = LEGACY[src]
        got = stores[pid].db.execute("SELECT count(*), count(v.rowid) FROM chunks c LEFT JOIN vec v ON v.rowid = c.rowid AND v.hash = c.hash WHERE c.source = ?", (sid,)).fetchone()
        assert got == (n_chunks, n_vec), f"{pid}/{sid}: {got} != {(n_chunks, n_vec)}"
        assert stores[pid].db.execute("SELECT count(*) FROM fts").fetchone()[0] == stores[pid].db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    for pid, st in stores.items():
        assert st.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    log("verified: counts, vectors and integrity match the legacy index")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--old")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg = config.load()
    migrate(cfg, a.old or cfg.data_path("search.db"), a.force)
