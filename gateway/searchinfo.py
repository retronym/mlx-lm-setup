"""Read-only view of the search index for the gateway: universes, projects and sources from the JSON config under pipelines/search, and what
each database holds. Starts nothing and loads no model. The config module is stdlib-only, so it is imported straight from the backend's
`index_dir` (the gateway's own venv needs none of the indexer's dependencies)."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


def load_config(index_dir: str, config_dir: str | None = None):
    """The validated search config (pipelines/search/config.py's `Config`), re-read on every call so edits show up without a restart."""
    key = f"_search_config_{abs(hash(index_dir))}"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, Path(index_dir) / "config.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod, mod.load(config_dir)


def universes(cfg) -> list[dict]:
    return [{"id": u.id, "title": u.title, "description": u.description, "default": u.default or u is cfg.default_universe(),
             "projects": [{"id": p, "title": cfg.projects[p].title,
                           "sources": [{"key": s.key, "id": s.id, "label": s.label, "color": s.color, "priority": s.priority, "type": s.type, "enabled": s.enabled}
                                       for s in cfg.projects[p].sources]} for p in u.projects]} for u in cfg.universes.values()]


def stats(cfg, universe_id: str | None = None) -> dict:
    """Per project and source: chunks, how many have a vector for the configured embedder, last write and sync position."""
    uni = cfg.universe(universe_id)
    model = cfg.search["embedder"]["model"]
    projects = []
    for pid in uni.projects:
        proj, db = cfg.projects[pid], cfg.project_db(pid)
        rows, state = {}, {}
        if db.exists():
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
            try:
                rows = {r[0]: r[1:] for r in con.execute("""SELECT c.source, count(*), count(v.rowid), max(c.updated) FROM chunks c
                                                            LEFT JOIN vec v ON v.rowid = c.rowid AND v.hash = c.hash AND v.model = ? GROUP BY c.source""", (model,))}
                state = {(s, k): v for s, k, v in con.execute("SELECT source, k, v FROM state WHERE k NOT LIKE 'file:%'")}
            finally:
                con.close()
        projects.append({"id": pid, "title": proj.title, "indexed": db.exists(), "sources": [
            {"key": s.key, "id": s.id, "label": s.label, "color": s.color, "priority": s.priority, "type": s.type, "enabled": s.enabled,
             "chunks": rows.get(s.id, (0, 0, None))[0], "embedded": rows.get(s.id, (0, 0, None))[1], "updated": rows.get(s.id, (0, 0, None))[2],
             "position": (state.get((s.id, "head")) or "")[:10] or state.get((s.id, "since_issues")) or state.get((s.id, "last_release"))}
            for s in proj.sources]})
    return {"universe": {"id": uni.id, "title": uni.title}, "projects": projects}
