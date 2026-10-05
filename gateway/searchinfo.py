"""Read-only view of the search index for the gateway: universes, projects and sources from the JSON config under pipelines/search, and what
each database holds. Starts nothing and loads no model. The config module is stdlib-only, so it is imported straight from the backend's
`index_dir` (the gateway's own venv needs none of the indexer's dependencies)."""
from __future__ import annotations

import calendar
import importlib.util
import sqlite3
import sys
import time
from pathlib import Path


def _module(index_dir: str, name: str):
    key = f"_search_{name}_{abs(hash(index_dir))}"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, Path(index_dir) / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


def load_config(index_dir: str, config_dir: str | None = None):
    """The validated search config (pipelines/search/config.py's `Config`), re-read on every call so edits show up without a restart."""
    mod = _module(index_dir, "config")
    return mod, mod.load(config_dir)


def run_state(index_dir: str, cfg) -> dict | None:
    """What sync.py / embed.py are doing right now (pipelines/search/runstate.py), or None if nothing ever ran."""
    return _module(index_dir, "runstate").read(cfg)


def universes(cfg) -> list[dict]:
    return [{"id": u.id, "title": u.title, "description": u.description, "default": u.default or u is cfg.default_universe(),
             "projects": [{"id": p, "title": cfg.projects[p].title,
                           "sources": [{"key": s.key, "id": s.id, "label": s.label, "color": s.color, "priority": s.priority, "type": s.type, "enabled": s.enabled}
                                       for s in cfg.projects[p].sources]} for p in u.projects]} for u in cfg.universes.values()]


def _epoch(iso: str | None) -> float | None:
    try:
        return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) if iso else None
    except ValueError:
        return None


def _github_progress(src, state: dict, now: float) -> dict:
    """How far each stream of a GitHub source has walked from its horizon (`since`) to now. A stream is done when its backfill reached the
    horizon; while backfilling newest-first, the fraction is (top - frontier) / (top - horizon). A stream written by the older single-cursor
    sync (ascending from the horizon) is measured by where that cursor is."""
    horizon = _epoch(src.since) or 0.0
    owner = f"gh:{src.repo}"
    kinds = [("issues & PRs", "issues", {"issues", "prs"}), ("comments", "comments", {"comments"}), ("review comments", "reviews", {"reviews"})]
    streams = []
    for name, key, needs in kinds:
        if not needs & set(src.include):
            continue
        get = lambda k: state.get((owner, f"{k}_{key}")) or state.get((src.id, f"{k}_{key}"))
        top, back, done, fwd = _epoch(get("top")), _epoch(get("back")), get("bf_done"), _epoch(get("fwd") or state.get((src.id, f"since_{key}")))
        if done == "1":
            frac, at, mode = 1.0, src.since, "done"
        elif top and back and top > horizon:
            frac, at, mode = (top - back) / (top - horizon), get("back"), "back"
        elif fwd and now > horizon:
            frac, at, mode = (fwd - horizon) / (now - horizon), get("fwd") or state.get((src.id, f"since_{key}")), "fwd"
        else:
            frac, at, mode = 0.0, None, None
        streams.append({"name": name, "frac": max(0.0, min(1.0, frac)), "at": at, "mode": mode})
    return {"kind": "timeline", "horizon": (src.since or "")[:10], "streams": streams,
            "frac": min((x["frac"] for x in streams), default=1.0)}


def stats(cfg, universe_id: str | None = None, run: dict | None = None) -> dict:
    """Per project and source: chunks, how many have a vector for the configured embedder, last write, sync position and progress. `run` is the
    indexer's current run state; the source it is working on is marked active."""
    uni = cfg.universe(universe_id)
    model = cfg.search["embedder"]["model"]
    now = time.time()
    working = run["source"] if run and run.get("running") else None
    projects = []
    for pid in uni.projects:
        proj, db = cfg.projects[pid], cfg.project_db(pid)
        rows, state, files = {}, {}, {}
        if db.exists():
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
            try:
                rows = {r[0]: r[1:] for r in con.execute("""SELECT c.source, count(*), count(v.rowid), max(c.updated) FROM chunks c
                                                            LEFT JOIN vec v ON v.rowid = c.rowid AND v.hash = c.hash AND v.model = ? GROUP BY c.source""", (model,))}
                state = {(s, k): v for s, k, v in con.execute("SELECT source, k, v FROM state WHERE k NOT LIKE 'file:%'")}
                files = dict(con.execute("SELECT source, count(*) FROM state WHERE k LIKE 'file:%' GROUP BY source").fetchall())
            finally:
                con.close()
        out = []
        for s in proj.sources:
            chunks, embedded, updated = rows.get(s.id, (0, 0, None))
            if s.type == "git":
                total = int(state.get((s.id, "files_total")) or 0)
                sync = {"kind": "files", "done": files.get(s.id, 0), "total": total, "frac": min(1.0, files.get(s.id, 0) / total) if total else (1.0 if files.get(s.id) else 0.0)}
            elif s.type == "github":
                sync = _github_progress(s, state, now)
            else:
                sync = {"kind": "snapshot", "frac": 1.0 if chunks else 0.0}
            out.append({"key": s.key, "id": s.id, "label": s.label, "color": s.color, "priority": s.priority, "type": s.type, "enabled": s.enabled,
                        "chunks": chunks, "embedded": embedded, "updated": updated, "sync": sync, "active": s.key == working,
                        "position": (state.get((s.id, "head")) or "")[:10] or state.get((s.id, "since_issues")) or state.get((s.id, "last_release"))})
        projects.append({"id": pid, "title": proj.title, "indexed": db.exists(), "sources": out})
    return {"universe": {"id": uni.id, "title": uni.title}, "projects": projects}
