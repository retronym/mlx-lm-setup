"""The dashboard's facts layer: what happened in the selected projects over a bounded range, and the state of every open PR. Pure SQL over the project
databases (stdlib only, the gateway imports it), no models, so it answers in about a second and is exact. Model-written parts (labels, summaries) are
layered on top by other jobs and cached; nothing here waits for them.

Dates: a PR or issue's `updated` is when it last moved, so "merged" and "closed" in the range mean "now merged / closed and last touched in the range"
(a merge is almost always the last event), "opened" is `created`. Comments count by their own `created`. Open PRs ignore the range except to flag
the ones touched in it: triage is about what is open today.

Open PRs carry `pr_state` (sources/ghprstate.py, written by the nightly sync: draft, mergeability, CI, review decision, reviewers, size) when it has been
fetched; their `readiness` is a rule over it, with the reason, and falls back to age and activity when it is missing."""
import datetime as dt, json, re, sqlite3, time
from collections import Counter, defaultdict

MAX_DAYS = 366
DEFAULT_DAYS = 30
STALE_DAYS = 180
TOP = 10
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
READINESS = ("ready to merge", "needs author", "needs review", "in review", "draft", "stale")


def check_range(since=None, until=None, today=None):
    """(since, until) as YYYY-MM-DD, `until` exclusive; defaults to the last DEFAULT_DAYS days including today. ValueError for a malformed or too long range."""
    today = today or dt.date.today()
    for name, v in (("since", since), ("until", until)):
        if v and not _DAY.match(v):
            raise ValueError(f"{name} must be YYYY-MM-DD")
    end = dt.date.fromisoformat(until) if until else today + dt.timedelta(days=1)
    start = dt.date.fromisoformat(since) if since else end - dt.timedelta(days=DEFAULT_DAYS)
    if start >= end:
        raise ValueError("since must be before until")
    if (end - start).days > MAX_DAYS:
        raise ValueError(f"the range is limited to {MAX_DAYS} days")
    return start.isoformat(), end.isoformat()


def _bucket(day, weekly):
    d = dt.date.fromisoformat(day)
    return (d - dt.timedelta(days=d.weekday())).isoformat() if weekly else day


def _split_title(title):
    """('owner/repo', 'number', 'text') from 'owner/repo#123 text'."""
    m = re.match(r"^(\S+)#(\d+) (.*)$", title, re.S)
    return (m.group(1), m.group(2), m.group(3)) if m else ("", "", title)


def readiness(pr, now=None):
    """(label, reason) for an open PR. Rules first: they read the stored GitHub state when there is one, else only age and activity."""
    now = now or time.time()
    s = pr.get("state") or {}
    idle = (now - dt.datetime.fromisoformat(pr["updated"].replace("Z", "+00:00")).timestamp()) / 86400
    stale = idle > STALE_DAYS
    if s.get("draft"):
        return ("stale", f"draft, idle {int(idle)} days") if stale else ("draft", "draft")
    if s:
        if s.get("mergeable") == "CONFLICTING":
            return ("stale", f"conflicts, idle {int(idle)} days") if stale else ("needs author", "merge conflicts")
        if s.get("ci") == "fail":
            return ("stale", f"CI failing, idle {int(idle)} days") if stale else ("needs author", "CI failing")
        if s.get("decision") == "CHANGES_REQUESTED":
            return ("stale", f"changes requested, idle {int(idle)} days") if stale else ("needs author", "changes requested")
        if s.get("decision") == "APPROVED" and s.get("ci") in ("pass", "none") and s.get("mergeable") != "UNKNOWN":
            return "ready to merge", "approved, CI green, no conflicts"
    if stale:
        return "stale", f"idle {int(idle)} days"
    if not pr["comments"] and not s.get("reviews") and not s.get("decision"):
        return "needs review", "no reviews or comments yet"
    return "in review", "under discussion" if pr["comments"] else "reviewed"


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


ACT = "COALESCE(json_extract(meta, '$.updated'), json_extract(meta, '$.published'), json_extract(meta, '$.created'))"      # the indexed expression (store.INDEXES)


def project_facts(db, since, until, weekly, now=None):
    """The activity of one project database in [since, until) and its open PRs."""
    con = _connect(db)
    try:
        rows = con.execute(f"""SELECT json_extract(meta, '$.kind'), json_extract(meta, '$.state'), json_extract(meta, '$.number'), json_extract(meta, '$.author'),
                                      json_extract(meta, '$.author_name'), json_extract(meta, '$.created'), json_extract(meta, '$.updated'), source, doc, id, title, url,
                                      json_extract(meta, '$.tag'), json_extract(meta, '$.published')
                               FROM chunks WHERE {ACT} >= ?""", (since,)).fetchall()
        opens = con.execute("""SELECT source, doc, title, url, json_extract(meta, '$.number'), json_extract(meta, '$.author'), json_extract(meta, '$.created'),
                                      json_extract(meta, '$.updated'), json_extract(meta, '$.labels'), json_extract(meta, '$.pr_state'), json_extract(meta, '$.closes')
                               FROM chunks WHERE json_extract(meta, '$.kind') = 'pr' AND json_extract(meta, '$.state') = 'open' AND id NOT LIKE '%~%'""").fetchall()
        threads = {}                                                       # (source, doc) -> total comment and review chunks, for the open PRs
        if opens:
            marks = ",".join("(?, ?)" for _ in opens)
            for src, doc, n in con.execute(f"""SELECT source, doc, count(*) FROM chunks WHERE json_extract(meta, '$.kind') IN ('comment', 'review')
                                               AND (source, doc) IN (VALUES {marks}) GROUP BY source, doc""", [x for o in opens for x in o[:2]]):
                threads[(src, doc)] = n
    finally:
        con.close()
    inr = lambda t: bool(t) and since <= t[:10] < until
    tot, series = Counter(), defaultdict(Counter)
    authors, hot, newest, releases, touched = defaultdict(Counter), Counter(), [], [], set()
    heads = {}
    for kind, state, num, author, aname, created, updated, src, doc, cid, title, url, tag, published in rows:
        if kind in ("issue", "pr") and "~" not in cid:
            heads[(src, doc)] = (kind, state, title, url, author, created, updated)
    for kind, state, num, author, aname, created, updated, src, doc, cid, title, url, tag, published in rows:
        if kind in ("issue", "pr") and "~" not in cid:
            k = "prs" if kind == "pr" else "issues"
            if inr(created):
                tot[f"{k}_opened"] += 1; series[_bucket(created[:10], weekly)][f"{k}_opened"] += 1
                authors[author or "?"][f"{k}_opened"] += 1
                if kind == "issue":
                    newest.append((created, title, url, author, state))
            if inr(updated):
                touched.add((src, doc))
                if state == "merged":
                    tot["prs_merged"] += 1; series[_bucket(updated[:10], weekly)]["prs_merged"] += 1; authors[author or "?"]["prs_merged"] += 1
                elif state == "closed":
                    tot[f"{k}_closed"] += 1
        elif kind in ("comment", "review") and inr(created):
            tot["comments"] += 1; hot[(src, doc)] += 1; authors[author or "?"]["comments"] += 1
        elif kind == "commit" and "~" not in cid and inr(created):
            tot["commits"] += 1; series[_bucket(created[:10], weekly)]["commits"] += 1; authors[author or aname or "?"]["commits"] += 1
        elif kind in ("release", "tag") and cid.endswith(":header") if kind == "release" else kind == "tag":
            if inr(published or created):
                releases.append({"tag": tag, "title": title, "url": url, "date": (published or created)[:10], "kind": kind})
    hot_threads, top = [], hot.most_common(TOP)
    old = [k for k, _ in top if k not in heads]                            # a thread opened before the range, with comments in it: its head is not among `rows`
    if old:
        con = _connect(db)
        try:
            for src, doc in old:
                r = con.execute("""SELECT json_extract(meta, '$.kind'), json_extract(meta, '$.state'), title, url FROM chunks WHERE source = ? AND doc = ?
                                   AND json_extract(meta, '$.kind') IN ('issue', 'pr') AND id NOT LIKE '%~%'""", (src, doc)).fetchone()
                if r:
                    heads[(src, doc)] = (r[0], r[1], r[2], r[3], None, None, None)
        finally:
            con.close()
    for (src, doc), n in top:
        h = heads.get((src, doc))
        if h:
            hot_threads.append({"title": _split_title(h[2])[2], "repo": _split_title(h[2])[0], "number": _split_title(h[2])[1], "url": h[3], "kind": h[0], "state": h[1], "comments": n})
    out_open = []
    for src, doc, title, url, num, author, created, updated, labels, state, closes in opens:
        repo, n, text = _split_title(title)
        pr = {"repo": repo, "number": num, "title": text, "url": url, "author": author, "created": created, "updated": updated, "labels": json.loads(labels or "[]"),
              "state": json.loads(state) if state else None, "closes": json.loads(closes or "[]"), "comments": threads.get((src, doc), 0), "touched": (src, doc) in touched}
        pr["readiness"], pr["reason"] = readiness(pr, now)
        out_open.append(pr)
    return {"totals": dict(tot), "series": {k: dict(v) for k, v in series.items()}, "authors": authors, "hot_threads": hot_threads,
            "new_issues": [{"created": c, "title": _split_title(t)[2], "repo": _split_title(t)[0], "number": _split_title(t)[1], "url": u, "author": a, "state": s}
                           for c, t, u, a, s in sorted(newest, reverse=True)[:30]],
            "releases": releases, "open_prs": out_open}


def twins(cfg, uni, keys, min_sim=0.9):
    """{"repo#number": [{ref, title, state, url, sim}]} up to two of the nearest items (issues and PRs, any state) at least `min_sim` similar, for the wanted
    keys, from the neighbours database the refresh builds (nothing when it has not been built)."""
    path = cfg.data_path("neighbours", f"{uni.id}.db")
    if not keys or not path.exists():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        keys = list(dict.fromkeys(keys))
        mine = {}                                                          # idx -> ref, for the wanted items (an item's title starts with its ref)
        for i in range(0, len(keys), 400):
            part = keys[i:i + 400]
            mine.update(con.execute(f"SELECT idx, substr(title, 1, instr(title, ' ') - 1) FROM items WHERE kind IN ('issue', 'pr') "
                                    f"AND substr(title, 1, instr(title, ' ') - 1) IN ({','.join('?' * len(part))})", part).fetchall())
        pairs = [(a, b, sim) for a, b, sim in con.execute("SELECT a, b, sim FROM pairs WHERE sim >= ? ORDER BY sim DESC", (min_sim,)) if a in mine or b in mine]
        info = {idx: (title, state, url, kind) for idx, title, state, url, kind in con.execute(
            f"SELECT idx, title, state, url, kind FROM items WHERE idx IN ({','.join('?' * len({x for p in pairs for x in p[:2]}))})", sorted({x for p in pairs for x in p[:2]}))} if pairs else {}
        out = defaultdict(list)
        for a, b, sim in pairs:
            for me, other in ((a, b), (b, a)):
                if me in mine and other in info and len(out[mine[me]]) < 2:
                    title, state, url, kind = info[other]
                    out[mine[me]].append({"ref": title.split(" ", 1)[0], "title": title.split(" ", 1)[1] if " " in title else "", "state": state, "url": url, "kind": kind, "sim": round(sim, 3)})
        return dict(out)
    finally:
        con.close()


def facts(cfg, universe_id=None, projects=None, since=None, until=None, now=None):
    """The dashboard's facts layer for `projects` (default: every project of the universe) over [since, until) (see `check_range`)."""
    since, until = check_range(since, until)
    uni = cfg.universe(universe_id)
    chosen = [p for p in uni.projects if not projects or p in projects]
    if projects and set(projects) - set(uni.projects):
        raise ValueError(f"unknown projects: {sorted(set(projects) - set(uni.projects))}")
    weekly = (dt.date.fromisoformat(until) - dt.date.fromisoformat(since)).days > 92
    t0 = time.time()
    tot, series, authors, hot, new, rel, opens, per = Counter(), defaultdict(Counter), defaultdict(Counter), [], [], [], [], {}
    for pid in chosen:
        db = cfg.project_db(pid)
        if not db.exists():
            continue
        f = project_facts(db, since, until, weekly, now)
        per[pid] = {"title": cfg.projects[pid].title, **f["totals"], "open_prs": len(f["open_prs"])}
        tot.update(f["totals"])
        for b, c in f["series"].items():
            series[b].update(c)
        for a, c in f["authors"].items():
            authors[a].update(c)
        hot += [{**h, "project": pid} for h in f["hot_threads"]]
        new += [{**n, "project": pid} for n in f["new_issues"]]
        rel += [{**r, "project": pid} for r in f["releases"]]
        opens += [{**o, "project": pid} for o in f["open_prs"]]
    start, end = dt.date.fromisoformat(since), dt.date.fromisoformat(until)
    step = 7 if weekly else 1
    first = start - dt.timedelta(days=start.weekday()) if weekly else start
    buckets = [(first + dt.timedelta(days=i)).isoformat() for i in range(0, (end - first).days, step)]
    near = twins(cfg, uni, [f"{o['repo']}#{o['number']}" for o in opens] + [f"{n['repo']}#{n['number']}" for n in new])
    for o in opens:
        o["twins"] = near.get(f"{o['repo']}#{o['number']}", [])
    for n in new:
        n["twins"] = near.get(f"{n['repo']}#{n['number']}", [])
    fetched = [o["state"]["fetched"] for o in opens if o["state"]]
    return {"universe": uni.id, "projects": chosen, "since": since, "until": until, "bucket": "week" if weekly else "day", "totals": dict(tot), "by_project": per,
            "series": [{"date": b, **series.get(b, {})} for b in buckets], "releases": sorted(rel, key=lambda r: r["date"], reverse=True),
            "hot_threads": sorted(hot, key=lambda h: -h["comments"])[:TOP], "new_issues": sorted(new, key=lambda n: n["created"], reverse=True)[:30],
            "authors": [{"author": a, **c} for a, c in sorted(authors.items(), key=lambda kv: -sum(kv[1].values()))[:15] if a != "?"],
            "open_prs": sorted(opens, key=lambda o: o["updated"], reverse=True),
            "readiness": dict(Counter(o["readiness"] for o in opens)), "state_oldest": min(fetched) if fetched else None, "state_missing": sum(1 for o in opens if not o["state"]),
            "me": list(cfg.search.get("me", [])), "elapsed_s": round(time.time() - t0, 2)}
