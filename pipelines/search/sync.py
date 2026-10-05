#!/usr/bin/env python3
"""Sync sources into their project databases, in priority order.

usage: sync.py [universe | project | project/source ...] [--max-priority N] [--limit N] [--since ISO8601] [--force] [--no-fetch] [--reconcile] [--meta-only]

Default target: the default universe. Sources are processed by priority (1 first), skipping disabled ones and those synced more recently
than their min_interval_hours (--force ignores that). A failing source is reported and the rest continue (exit status 1 at the end).
--reconcile drops issues and PRs that no longer exist upstream."""
import sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
import config, repos, runstate
from embed import targets
from store import Store
from sources import ghissues
from sources.ghreleases import GhReleases
from sources.gitlog import GitLog
from sources.gitsrc import GitSource


def due(st, s, force):
    last = st.get(s.id, "last_sync")
    return force or not s.min_interval_hours or not last or time.time() - float(last) >= s.min_interval_hours * 3600


def run_sync(cfg, srcs, run, *, limit=None, since=None, force=False, no_fetch=False, reconcile=False, deadline=None, meta_only=False):
    """Sync `srcs` (already filtered and in priority order). Returns the number of sources that failed. After `deadline` (epoch seconds) no new
    source is started; the one running finishes. `meta_only` refreshes the metadata of chunks that already exist (GitHub items and commits are re-read
    from the part of history that is indexed; nothing new is ingested, no cursor moves, no minimum interval applies; git sources are skipped)."""
    ghissues.configure(cfg.search["github"])
    max_chars = cfg.search["chunking"]["max_chars"]
    fetched, failed, gh_done, skipped = set(), 0, set(), 0
    for s in srcs:
        if deadline and time.time() > deadline:
            skipped += 1
            continue
        st = Store(cfg.project_db(s.project))
        if s.type == "github":                                   # one pass over a repo's streams serves all its sources in the project
            if (s.project, s.repo) in gh_done:
                continue
            gh_done.add((s.project, s.repo))
            members = [m for m in srcs if m.type == "github" and (m.project, m.repo) == (s.project, s.repo)]
            if not (reconcile or meta_only) and not any(due(st, m, force) for m in members):      # a reconcile or metadata repair ignores the minimum interval
                run.log(f"{members[0].key}: synced recently, skipped")
                continue
            run.source("+".join(m.key for m in members), "sync")
            g = ghissues.GhRepo(members, max_chars)
            try:
                (g.reconcile(st, log=run.log) if reconcile else g.sync(st, since=since, limit=limit, log=run.log, meta_only=meta_only))
                for m in members:
                    st.put(m.id, "last_sync", str(time.time()))
                st.commit()
            except Exception as e:                               # noqa: BLE001  one source failing must not stop the rest
                failed += 1
                print(f"{g.key}: FAILED: {type(e).__name__}: {e}", file=sys.stderr)
                run.error(f"{g.key}: {type(e).__name__}: {e}")
            continue
        if meta_only and s.type == "git":
            continue
        if not meta_only and not due(st, s, force):
            run.log(f"{s.key}: synced less than {s.min_interval_hours:g} h ago, skipped")
            continue
        run.source(s.key, "sync")
        try:
            if s.type == "git":
                d = repos.ensure(cfg, s.repo, fetch=not no_fetch and s.repo not in fetched)
                fetched.add(s.repo)
                GitSource(s, d, max_chars).sync(st, limit=limit, log=run.log)
            elif s.type == "git_log":
                d = repos.ensure(cfg, s.repo, fetch=not no_fetch and s.repo not in fetched)
                fetched.add(s.repo)
                GitLog(s, d, max_chars).sync(st, limit=limit, log=run.log, meta_only=meta_only)
            elif s.type == "github_releases":
                d = repos.ensure(cfg, s.repo, fetch=not no_fetch and s.repo not in fetched) if s.tag_messages else None
                fetched.add(s.repo) if d else None
                GhReleases(s, d, max_chars).sync(st, log=run.log)
            else:
                run.log(f"{s.key}: source type {s.type!r} is not implemented yet, skipped")
                continue
            st.put(s.id, "last_sync", str(time.time())); st.commit()
        except Exception as e:                                           # noqa: BLE001
            failed += 1
            print(f"{s.key}: FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            run.error(f"{s.key}: {type(e).__name__}: {e}")
    if skipped:
        run.log(f"time budget reached: {skipped} source(s) not started, they come first next run")
    return failed


def main(argv):
    a = list(argv)
    def opt(name, default=None):
        if name in a:
            i = a.index(name); v = a[i + 1]; del a[i:i + 2]; return v
        return default
    limit, since, maxp = opt("--limit"), opt("--since"), int(opt("--max-priority", 9))
    force, no_fetch, reconcile, meta_only = [x in a for x in ("--force", "--no-fetch", "--reconcile", "--meta-only")]
    a = [x for x in a if not x.startswith("--")]
    cfg = config.load()
    srcs = targets(cfg, a, maxp)
    try:
        with runstate.lock(cfg):
            run = runstate.Run(cfg, "sync", [s.key for s in srcs])
            failed = run_sync(cfg, srcs, run, limit=int(limit) if limit else None, since=since, force=force, no_fetch=no_fetch, reconcile=reconcile, meta_only=meta_only)
            run.finish(not failed)
    except runstate.Busy as e:
        print(e, file=sys.stderr)
        return 3
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
