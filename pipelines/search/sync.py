#!/usr/bin/env python3
"""Sync sources into the search index.  usage: sync.py [source ...] [--limit N] [--since ISO8601]\n  sources: scalac scala3docs bug (scala/bug issues+comments) reconcile (drop deleted issues); default: all but reconcile"""
import sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from store import Store
from sources.gitsrc import SOURCES as GIT
from sources.ghissues import GhIssues

if __name__ == "__main__":
    args = sys.argv[1:]
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    since = args[args.index("--since") + 1] if "--since" in args else None
    names = [a for a in args if a in GIT or a in ("bug", "reconcile")] or list(GIT) + ["bug"]
    st = Store()
    for n in names:
        if n == "bug":
            GhIssues().sync(st, since=since, limit=limit)
        elif n == "reconcile":
            GhIssues().reconcile(st)
        else:
            GIT[n]().sync(st, limit=limit)
