#!/usr/bin/env python3
"""Lifecycle check on a scratch git repo and a scratch DB: add, edit one method, rename, delete, metadata-only change.
Asserts the sync touches only what changed. usage: python test_lifecycle.py"""
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from store import Store, Chunk
from sources.gitsrc import GitSource
import config

A = "package demo\nobject A {\n  def one = 1 // padding padding padding\n\n  def two = 2 + 2 // padding padding\n}\n"
B = "package demo\nclass B {\n  def hello(x: Int): Int = x + 1 // padding padding\n}\n"


def run(repo, *a):
    subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)


def sync(src, st):
    out = []
    src.sync(st, log=out.append)
    return out[-1].split("->")[1].strip()


with tempfile.TemporaryDirectory() as d:
    repo = os.path.join(d, "r"); os.mkdir(repo)
    run(repo, "init", "-q"); run(repo, "config", "user.email", "t@t"); run(repo, "config", "user.name", "t")
    w = lambda p, t: open(os.path.join(repo, p), "w").write(t)
    st = Store(os.path.join(d, "t.db"))
    cs = config.Source(project="t", id="t", type="git", label="t", color="#000000", priority=5, enabled=True, min_interval_hours=0, max_items_per_run=None,
                       repo="o/r", ref="HEAD", paths=(".",), chunkers={".scala": "scala"})
    src = GitSource(cs, repo)
    w("A.scala", A); w("B.scala", B); run(repo, "add", "."); run(repo, "commit", "-qm", "1")
    print("initial     ", r := sync(src, st)); assert r.startswith("+3 ~0 -0")
    print("no change   ", r := sync(src, st)); assert r.startswith("+0 ~0 -0 =0")        # nothing re-chunked at all
    w("A.scala", A.replace("2 + 2", "2 + 3")); run(repo, "commit", "-qam", "edit")
    print("edit one def", r := sync(src, st)); assert r.startswith("+0 ~1 -0 =")         # one chunk changed, the rest skipped
    run(repo, "mv", "B.scala", "C.scala"); run(repo, "commit", "-qam", "rename")
    print("rename      ", r := sync(src, st)); assert r.startswith("+1 ~0 -1")           # old path removed, new path added
    run(repo, "rm", "-q", "A.scala"); run(repo, "commit", "-qm", "delete")
    print("delete file ", r := sync(src, st)); assert "-2" in r
    assert st.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == st.db.execute("SELECT count(*) FROM fts").fetchone()[0]
    # metadata-only update (issue closed): same text, new meta -> row updated, hash unchanged so any vector stays valid
    c = Chunk("m:1", "issue:1", "t", "body", "u", {"state": "open"}); st.apply("m", [c])
    h = st.db.execute("SELECT hash FROM chunks WHERE id='m:1'").fetchone()
    st.apply("m", [Chunk("m:1", "issue:1", "t", "body", "u", {"state": "closed"})])
    assert st.db.execute("SELECT hash, meta FROM chunks WHERE id='m:1'").fetchone() == (h[0], '{"state": "closed"}')
    print("OK")
