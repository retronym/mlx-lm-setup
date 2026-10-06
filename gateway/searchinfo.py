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
                           "sources": [{"key": s.key, "id": s.id, "label": s.label, "color": s.color, "priority": s.priority, "type": s.type, "kinds": list(s.kinds), "enabled": s.enabled,
                            **({"site": s.repo} if s.type == "discourse" else {})}
                                       for s in cfg.projects[p].sources]} for p in u.projects]} for u in cfg.universes.values()]


def refresh_info(cfg) -> dict:
    """The latest digest and the last refresh runs (written by pipelines/search/refresh.py), for the status tab."""
    import json
    out = {"digest": None, "refresh": None}
    try:
        d = json.loads(cfg.data_path("digest.json").read_text())
        out["digest"] = {k: d.get(k) for k in ("universe", "generated", "since", "model", "checked", "overview", "attempts", "pruned", "facts", "text")}
    except (OSError, ValueError):
        pass
    try:
        r = json.loads(cfg.data_path("refresh.json").read_text())
        out["refresh"] = {"last_run": r.get("last_run"), "last_reconcile": r.get("last_reconcile"), "history": (r.get("history") or [])[:5]}
    except (OSError, ValueError):
        pass
    return out


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


def _commits_progress(src, state, now: float, name: str = "commits") -> dict:
    """How far the newest-first backfill of commits (or forum topics) has come: (top - back) / (top - horizon), complete when it reached the horizon or the end."""
    top, back, horizon = _epoch(state.get((src.id, "top_date"))), _epoch(state.get((src.id, "back_date"))), _epoch(src.since) or 0.0
    if state.get((src.id, "bf_done")) == "1":
        frac, at, mode = 1.0, src.since, "done"
    elif top and back and top > horizon:
        frac, at, mode = (top - back) / (top - horizon), state.get((src.id, "back_date")), "back"
    else:
        frac, at, mode = 0.0, None, None
    frac = max(0.0, min(1.0, frac))
    return {"kind": "timeline", "horizon": (src.since or "")[:10], "streams": [{"name": name, "frac": frac, "at": at, "mode": mode}], "frac": frac}


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
            elif s.type == "git_log":
                sync = _commits_progress(s, state, now)
            elif s.type == "discourse":
                sync = _commits_progress(s, state, now, "topics")
            else:
                sync = {"kind": "snapshot", "frac": 1.0 if chunks else 0.0}
            out.append({"key": s.key, "id": s.id, "label": s.label, "color": s.color, "priority": s.priority, "type": s.type, "enabled": s.enabled,
                        "chunks": chunks, "embedded": embedded, "updated": updated, "sync": sync, "active": s.key == working,
                        "position": (state.get((s.id, "head")) or "")[:10] or state.get((s.id, "since_issues")) or state.get((s.id, "last_release")) or (state.get((s.id, "fwd")) or "")[:10] or None})
        projects.append({"id": pid, "title": proj.title, "indexed": db.exists(), "sources": out})
    return {"universe": {"id": uni.id, "title": uni.title}, "projects": projects}


# ---- duplicates and clusters: read from <data>/neighbours/<universe>.db, written by pipelines/search/neighbours.py in the refresh ----
import json as _json
import re as _re

TEMPLATED = _re.compile(r"release procedure|#\d+ release (scala )?\d|\bupdate sbt\b|\bsbt \S+ \(was|bump|dummy ticket|\(issue was deleted\)", _re.I)
_ITEM = "idx, project, id, kind, state, created, number, author, title, url, cluster"
_KEYS = _ITEM.replace(" ", "").split(",")


def _neighbours_db(cfg, universe_id: str | None):
    uni = cfg.universe(universe_id)
    path = cfg.data_path("neighbours", f"{uni.id}.db")
    if not path.exists():
        return uni, None
    return uni, sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def _item(row) -> dict:
    return dict(zip(_KEYS, row))


def _date(s: str | None, name: str) -> str | None:
    if s in (None, ""):
        return None
    if not _re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", s):
        raise ValueError(f"{name} must be YYYY, YYYY-MM or YYYY-MM-DD")
    return s


def _meta(con) -> dict:
    return dict(con.execute("SELECT k, v FROM meta"))


def _item_filter(prefix: str, *, state, kind, since, until, projects) -> tuple[str, list]:
    """SQL on one item (`prefix` is its table alias) for the filters that apply to a single item. Used per item for clusters; for pairs the
    state, kind and project filters are about the pair, so they are built in `duplicates`."""
    cond, args = [], []
    if state == "open":
        cond.append(f"{prefix}.state = 'open'")
    elif state == "closed":
        cond.append(f"{prefix}.state != 'open'")
    if kind in ("issue", "pr"):
        cond.append(f"{prefix}.kind = ?"); args.append(kind)
    if since:
        cond.append(f"substr({prefix}.created, 1, 10) >= ?"); args.append(since)
    if until:
        cond.append(f"substr({prefix}.created, 1, 10) < ?"); args.append(until)
    if projects:
        cond.append(f"{prefix}.project IN ({','.join('?' * len(projects))})"); args += list(projects)
    return " AND ".join(cond) or "1", args


def duplicates(cfg, universe_id: str | None = None, *, min_sim: float = 0.9, state: str = "any", kind: str = "issue", since: str | None = None,
               until: str | None = None, projects: list[str] | None = None, adjacent: int = 1, templated: bool = False, limit: int = 100, offset: int = 0) -> dict:
    """Pairs of issues / PRs whose vectors are close, best first.
      state     any | open (at least one of the pair is open) | closed (neither is)
      kind      issue | pr (both are) | mixed (one issue, one PR: a fix that probably exists) | any
      since/until  YYYY[-MM[-DD]]: a pair is kept if EITHER item was created in the range
      projects  a pair is kept if EITHER item is in one of them
      adjacent  drop same-repo pairs whose numbers differ by at most this when one of them is closed: an old import created adjacent copies
                (0 keeps them)
      templated keep release-procedure and dependency-bump pairs, which look alike by construction"""
    if state not in ("any", "open", "closed") or kind not in ("issue", "pr", "mixed", "any"):
        raise ValueError("state must be any, open or closed; kind issue, pr, mixed or any")
    since, until = _date(since, "since"), _date(until, "until")
    uni, con = _neighbours_db(cfg, universe_id)
    if con is None:
        return {"available": False, "universe": uni.id, "total": 0, "pairs": []}
    try:
        con.create_function("templated", 2, lambda x, y: int(bool(TEMPLATED.search(f"{x} {y}"))), deterministic=True)
        cond, args = ["p.sim >= ?"], [min_sim]
        if kind == "mixed":
            cond.append("a.kind != b.kind")
        elif kind != "any":
            cond.append("a.kind = ? AND b.kind = ?"); args += [kind, kind]
        if state == "open":
            cond.append("(a.state = 'open' OR b.state = 'open')")
        elif state == "closed":
            cond.append("a.state != 'open' AND b.state != 'open'")
        if adjacent:
            cond.append("NOT (a.project = b.project AND a.kind = b.kind AND abs(a.number - b.number) <= ? AND (a.state != 'open' OR b.state != 'open'))"); args.append(adjacent)
        if since or until:
            ca, aa = _item_filter("a", state="any", kind="any", since=since, until=until, projects=None)
            cb, ab = _item_filter("b", state="any", kind="any", since=since, until=until, projects=None)
            cond.append(f"(({ca}) OR ({cb}))"); args += aa + ab
        if projects:
            marks = ",".join("?" * len(projects))
            cond.append(f"(a.project IN ({marks}) OR b.project IN ({marks}))"); args += list(projects) * 2
        if not templated:
            cond.append("NOT templated(a.title, b.title)")
        where = " AND ".join(cond)
        join = "FROM pairs p JOIN items a ON a.idx = p.a JOIN items b ON b.idx = p.b"
        total = con.execute(f"SELECT count(*) {join} WHERE {where}", args).fetchone()[0]
        cols = ", ".join(f"{t}.{c}" for t in "ab" for c in _KEYS)
        rows = con.execute(f"SELECT p.sim, {cols} {join} WHERE {where} ORDER BY p.sim DESC, p.a, p.b LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        n = len(_KEYS)
        pairs = [{"sim": r[0], "a": _item(r[1:1 + n]), "b": _item(r[1 + n:])} for r in rows]
        return {"available": True, "universe": uni.id, "total": total, "offset": offset, "pairs": pairs,
                "generated": float(_meta(con).get("generated") or 0) or None}
    finally:
        con.close()


def clusters(cfg, universe_id: str | None = None, *, state: str = "any", kind: str = "any", since: str | None = None, until: str | None = None,
             projects: list[str] | None = None, cluster: int | None = None, limit: int = 100, offset: int = 0) -> dict:
    """Topic clusters of issues and PRs with their counts under the filters (an item is kept or not on its own: state any | open | closed,
    kind issue | pr | any, created since/until, repo). `recent` counts items created in the last two years, `recent_share` is the same share over
    everything matching, so recent / size / recent_share above 1 means the topic is heating up. With `cluster`, that cluster's matching items, newest first."""
    if state not in ("any", "open", "closed") or kind not in ("issue", "pr", "any"):
        raise ValueError("state must be any, open or closed; kind issue, pr or any")
    since, until = _date(since, "since"), _date(until, "until")
    uni, con = _neighbours_db(cfg, universe_id)
    if con is None:
        return {"available": False, "universe": uni.id, "clusters": []}
    try:
        where, args = _item_filter("i", state=state, kind=kind, since=since, until=until, projects=projects)
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 2 * 365 * 86400))
        if cluster is not None:
            rows = con.execute(f"SELECT {', '.join('i.' + c for c in _KEYS)} FROM items i WHERE i.cluster = ? AND {where} ORDER BY i.created DESC LIMIT ? OFFSET ?",
                               [cluster, *args, limit, offset]).fetchall()
            total = con.execute(f"SELECT count(*) FROM items i WHERE i.cluster = ? AND {where}", [cluster, *args]).fetchone()[0]
            return {"available": True, "universe": uni.id, "cluster": cluster, "total": total, "offset": offset, "items": [_item(r) for r in rows]}
        counts = {r[0]: r[1:] for r in con.execute(
            f"SELECT i.cluster, count(*), sum(i.state = 'open'), sum(substr(i.created, 1, 10) >= ?) FROM items i WHERE {where} GROUP BY i.cluster", [cutoff, *args])}
        n = sum(c[0] for c in counts.values())
        share = sum(c[2] or 0 for c in counts.values()) / n if n else 0.0
        out = []
        for k, label, samples in con.execute("SELECT k, label, samples FROM clusters ORDER BY k"):
            if k not in counts:
                continue
            size, opened, recent = counts[k]
            ids = _json.loads(samples)
            by = {r[0]: _item(r) for r in con.execute(f"SELECT {_ITEM} FROM items WHERE idx IN ({','.join('?' * len(ids))})", ids)} if ids else {}
            out.append({"k": k, "label": label, "size": size, "open": opened or 0, "recent": recent or 0,
                        "trend": round((recent or 0) / size / share, 2) if size and share else None,
                        "projects": dict(con.execute(f"SELECT i.project, count(*) FROM items i WHERE i.cluster = ? AND {where} GROUP BY i.project", [k, *args]).fetchall()),
                        "samples": [by[i] for i in ids if i in by]})
        out.sort(key=lambda c: -c["size"])
        return {"available": True, "universe": uni.id, "items": n, "recent_share": round(share, 4), "clusters": out,
                "generated": float(_meta(con).get("generated") or 0) or None}
    finally:
        con.close()


def outliers(cfg, universe_id: str | None = None, *, by: str = "iso", state: str = "any", kind: str = "any", since: str | None = None, until: str | None = None,
             projects: list[str] | None = None, templated: bool = False, limit: int = 50, offset: int = 0) -> dict:
    """Issues and PRs that are far from everything else, most outlying first. `by` is the score: iso (cosine to the nearest other item) or ctr (cosine
    to the centre of the item's own cluster); lower means more of an outlier. Filters apply per item as for clusters (state any | open | closed,
    kind issue | pr | any, since / until, projects). Dependency bumps and release procedures are hidden unless `templated`: they are unlike
    anything else, and uninteresting."""
    if by not in ("iso", "ctr") or state not in ("any", "open", "closed") or kind not in ("issue", "pr", "any"):
        raise ValueError("by must be iso or ctr; state any, open or closed; kind issue, pr or any")
    since, until = _date(since, "since"), _date(until, "until")
    uni, con = _neighbours_db(cfg, universe_id)
    if con is None or "iso" not in {r[1] for r in con.execute("PRAGMA table_info(items)")}:
        return {"available": False, "universe": uni.id, "total": 0, "items": []}
    try:
        con.create_function("templated", 1, lambda t: int(bool(TEMPLATED.search(t or ""))), deterministic=True)
        where, args = _item_filter("i", state=state, kind=kind, since=since, until=until, projects=projects)
        if not templated:
            where += " AND NOT templated(i.title)"
        total = con.execute(f"SELECT count(*) FROM items i WHERE {where}", args).fetchone()[0]
        rows = con.execute(f"""SELECT {', '.join('i.' + c for c in _KEYS)}, i.iso, i.ctr, c.label FROM items i LEFT JOIN clusters c ON c.k = i.cluster
                               WHERE {where} ORDER BY i.{by}, i.idx LIMIT ? OFFSET ?""", [*args, limit, offset]).fetchall()
        n = len(_KEYS)
        items = [{**_item(r[:n]), "iso": r[n], "ctr": r[n + 1], "label": r[n + 2]} for r in rows]
        return {"available": True, "universe": uni.id, "by": by, "total": total, "offset": offset, "items": items, "generated": float(_meta(con).get("generated") or 0) or None}
    finally:
        con.close()
