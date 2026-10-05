#!/usr/bin/env python3
"""Sync sources into their project databases, in priority order.

usage: sync.py [universe | project | project/source ...] [--max-priority N] [--limit N] [--since ISO8601] [--force] [--no-fetch] [--reconcile]

Default target: the default universe. Sources are processed by priority (1 first), skipping disabled ones and those synced more recently
than their min_interval_hours (--force ignores that). A failing source is reported and the rest continue (exit status 1 at the end).
--reconcile drops issues and PRs that no longer exist upstream."""
import sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
import config, repos, runstate
from embed import targets
from store import Store
from sources import ghissues
from sources.gitsrc import GitSource


def due(st, s, force):
    last = st.get(s.id, "last_sync")
    return force or not s.min_interval_hours or not last or time.time() - float(last) >= s.min_interval_hours * 3600


def main(argv):
    a = list(argv)
    def opt(name, default=None):
        if name in a:
            i = a.index(name); v = a[i + 1]; del a[i:i + 2]; return v
        return default
    limit, since, maxp = opt("--limit"), opt("--since"), int(opt("--max-priority", 9))
    force, no_fetch, reconcile = [x in a for x in ("--force", "--no-fetch", "--reconcile")]
    a = [x for x in a if not x.startswith("--")]
    cfg = config.load()
    ghissues.configure(cfg.search["github"])
    max_chars = cfg.search["chunking"]["max_chars"]
    fetched, failed = set(), 0
    srcs = targets(cfg, a, maxp)
    run = runstate.Run(cfg, "sync", [s.key for s in srcs])
    for s in srcs:
        st = Store(cfg.project_db(s.project))
        if not due(st, s, force):
            run.log(f"{s.key}: synced less than {s.min_interval_hours:g} h ago, skipped")
            continue
        run.source(s.key)
        try:
            if s.type == "git":
                d = repos.ensure(cfg, s.repo, fetch=not no_fetch and s.repo not in fetched)
                fetched.add(s.repo)
                GitSource(s, d, max_chars).sync(st, limit=int(limit) if limit else None, log=run.log)
            elif s.type == "github":
                g = ghissues.GhIssues(s, max_chars)
                (g.reconcile(st, log=run.log) if reconcile else g.sync(st, since=since, limit=int(limit) if limit else None, log=run.log))
            else:
                run.log(f"{s.key}: source type {s.type!r} is not implemented yet, skipped")
                continue
            st.put(s.id, "last_sync", str(time.time())); st.commit()
        except Exception as e:                                           # noqa: BLE001  one source failing must not stop the rest
            failed += 1
            print(f"{s.key}: FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            run.error(f"{s.key}: {type(e).__name__}: {e}")
    run.finish(not failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
