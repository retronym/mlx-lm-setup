#!/usr/bin/env python3
"""Hybrid search over a universe: every member project has its own database; each is queried with BM25 and (when the query is embedded)
by vector cosine, the per-store hits are merged by score into one keyword list and one vector list (cosine is comparable across
projects because a universe shares one embedding model), the two lists are fused by reciprocal rank, and the best candidates are
optionally reranked by a cross-encoder.

usage: search.py [--universe U] [--project P]... [--source S]... [--kind K]... [--since YYYY[-MM[-DD]]] [--until ..] [--date created|updated] [--author A]...
                 [-k 8] [--open] [--bm25|--vec] [--rerank] [--explain] [--sort relevance|recent] <query>
       (an empty query lists what the filters match, newest first)
       (a --source is an id like `issues`, or `project/source`; a --kind is one of store.KINDS, like `commit` or `file`)"""
import json, re, sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
import config
from linkdb import LinkDB, NAMES, INV, SAY, TYPES, node_id, doc_names
from store import Store, _CAMEL, STATE_SQL, DATE_SQL, chunk_filter, check_where

STOP = set("a an the of in on to is are was be for and or not with how what where why does do this that it as by from at".split())


class Index:
    """The existing databases of one universe's projects (a project that has not been indexed yet is listed in `missing`)."""

    def __init__(self, cfg, universe_id=None):
        self.cfg, self.universe = cfg, cfg.universe(universe_id)
        self.stores, self.missing = {}, []
        self.links = LinkDB.open(cfg, self.universe.id)
        for pid in self.universe.projects:
            db = cfg.project_db(pid)
            (self.stores.__setitem__(pid, Store(db)) if db.exists() else self.missing.append(pid))

    def source(self, pid, sid):
        return self.cfg.projects[pid].source(sid)

    def close(self):
        """Close the databases: a long-running server makes an Index per request, and connections left to the garbage collector pile up against the fd limit."""
        for st in self.stores.values():
            st.db.close()
        if self.links:
            self.links.close()


def fts_query(q):
    """OR of the query's words and their camelCase parts."""
    terms = []
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", q):
        for t in [w] + (_CAMEL.split(w) if len(w) > 3 else []):
            t = t.lower()
            if len(t) > 1 and t not in STOP and t not in terms:
                terms.append(t)
    return " OR ".join(f'"{t}"' for t in terms)


def bm25(st, q, k, sources=None, open_only=False, kinds=None, linked=None, where=None):
    """[(rowid, bm25 score)] best first (FTS5 scores are negative: lower is better)."""
    fq = fts_query(q)
    if not fq:
        return []
    cond, args = chunk_filter(sources, open_only, kinds, linked, where)
    sql = f"SELECT c.rowid, bm25(fts, 3.0, 1.0) s FROM fts JOIN chunks c ON c.rowid = fts.rowid WHERE fts MATCH ? {cond} ORDER BY s LIMIT ?"
    return [(r[0], r[1]) for r in st.db.execute(sql, (fq, *args, k))]


FUSION = {"k": 60, "top_bonus": [0.05, 0.02, 0.02]}                     # reciprocal rank fusion; the bonus goes to ranks 1, 2, 3 of ANY list
BLEND = [[3, 0.75], [10, 0.6], [1000, 0.4]]                              # [up to this fused rank, weight of the retrieval score]; the rest is the reranker's


def fuse_scores(rankings, k=60, top_bonus=()):
    """{key: (rrf score, bonus part)}: sum of 1/(k+rank) over the lists, plus `top_bonus[i]` for being at rank i+1 in any list. With k=60 a
    first place is worth 0.016, so a 0.05 bonus makes an exact keyword or vector top hit hard to dislodge: it keeps `trait extraHash`
    on top however many fuzzy neighbours the other list brings."""
    score, bonus = {}, {}
    for r in rankings:
        for i, key in enumerate(r):
            score[key] = score.get(key, 0) + 1 / (k + i + 1)
            if i < len(top_bonus):
                bonus[key] = max(bonus.get(key, 0), top_bonus[i])
    return {key: (v + bonus.get(key, 0), bonus.get(key, 0)) for key, v in score.items()}


def fuse(rankings, k=60, top_bonus=()):
    sc = fuse_scores(rankings, k, top_bonus)
    return sorted(sc, key=lambda key: sc[key][0], reverse=True)


def blend_weight(rank, blend):
    """Weight of the retrieval score for a hit at fused rank `rank` (1-based): the first [limit, weight] row whose limit covers it."""
    for limit, w in blend:
        if rank <= limit:
            return w
    return blend[-1][1]


def _source_ok(idx, stores, key, sources):
    pid, rid = key
    sid = stores[pid].db.execute("SELECT source FROM chunks WHERE rowid = ?", (rid,)).fetchone()[0]
    f = _source_filter(sources, pid)
    return f is None or sid in f


def _source_filter(sources, pid):
    """Which source ids of project `pid` a `sources` filter ([id | project/source]) selects: None = all, [] = none."""
    if not sources:
        return None
    return sorted({s.split("/", 1)[1] if "/" in s else s for s in sources if "/" not in s or s.split("/", 1)[0] == pid})


def node_of(idx, key):
    """The links node of a search candidate `(project, rowid)`, or None."""
    pid, rid = key
    sid, kind_meta, doc = idx.stores[pid].db.execute("SELECT source, meta, doc FROM chunks WHERE rowid = ?", (rid,)).fetchone()
    m = json.loads(kind_meta)
    src = idx.source(pid, sid)
    return node_id(src.repo, m.get("kind") or "file", m, doc) if src is not None else None


def link_filter(idx, linked_to=None, link_type=None, has_link=None):
    """The `linked_to` / `link_type` / `has_link` request as one document restriction: ({"mode": "in" | "out", "nodes": {id: (kind, repo, ref)}} or None, what
    it resolved to). `linked_to` names documents (a hit `ref`, `scala/bug#123`, `#123`, a sha) and keeps those linked to any of them, through `link_type`
    (relation names like `closed_by`; name both ends, `closes` and `closed_by`, for either direction); `has_link` is a list of relations the document must have, `no_<relation>` that it must
    not. Raises ValueError for what it cannot make sense of; without a links database, a filter matches nothing."""
    if not (linked_to or has_link):
        if link_type:
            raise ValueError("link_type needs linked_to")
        return None, {}
    pos, neg, info = [], {}, {}
    if linked_to:
        named = [n for t in ([linked_to] if isinstance(linked_to, str) else linked_to) for n in (idx.links.resolve(t) if idx.links else [])]
        info["linked_to"] = [n["id"] for n in named]
        pos.append(idx.links.linked_to({n["id"] for n in named}, link_type) if idx.links and named else {})
    for name in has_link or []:
        neg_name = name.startswith("no_")
        if (name[3:] if neg_name else name) not in NAMES:
            raise ValueError(f"has_link: unknown relation {name!r} (have: {', '.join(NAMES)}, each also as no_<relation>)")
        found = idx.links.nodes_with(name[3:] if neg_name else name) if idx.links else {}
        if neg_name:
            neg.update(found)
        else:
            pos.append(found)
    if pos:
        keep = set.intersection(*(set(p) for p in pos))
        return {"mode": "in", "nodes": {i: v for i, v in pos[0].items() if i in keep and i not in neg}}, info
    return {"mode": "out", "nodes": neg}, info


def _link_pairs(cfg, pid, nodes):
    by_repo = {}
    for kind, repo, ref in nodes.values():
        by_repo.setdefault(repo, []).extend(doc_names(kind, ref))
    return [(s.id, d) for s in cfg.projects[pid].sources for d in by_repo.get(s.repo, ())]


def _pinned(idx, stores, named, cond, args, limit=30):
    """Rows of the documents `named` (nodes a query spells out as a reference) and of what links to them, the named ones first: a ranking of its own."""
    order = {n["id"]: n for n in named}
    for n in named:
        for r in idx.links.neighbours(n["id"], [x for x in NAMES if x not in ("touches", "touched_by", "ships")], limit):
            if r["node"]["indexed"] and r["node"]["id"] not in order:
                order[r["node"]["id"]] = r["node"]
    out = []
    for n in list(order.values())[:limit]:
        for pid, st in stores.items():
            for s in idx.cfg.projects[pid].sources:
                if s.repo != n["repo"]:
                    continue
                for doc in doc_names(n["kind"], n["ref"]):
                    row = st.db.execute(f"SELECT c.rowid FROM chunks c WHERE c.source = ? AND c.doc = ? {cond} ORDER BY c.rowid LIMIT 1", (s.id, doc, *args)).fetchone()
                    if row and (pid, row[0]) not in out:
                        out.append((pid, row[0]))
    return out


def _date_of(idx, key, field):
    pid, rid = key
    return idx.stores[pid].db.execute(f"SELECT {DATE_SQL[field]} FROM chunks c WHERE rowid = ?", (rid,)).fetchone()[0] or ""


def browse(idx, k=8, projects=None, sources=None, open_only=False, kinds=None, link_filter=None, where=None):
    """The documents matching the filters alone, newest first (by `where["date"]`, created by default), one per document (its newest matching chunk): what
    an empty query with filters means ("everything by me this year"). Undated chunks (files of a git tree) come last."""
    field = (where or {}).get("date", "created")
    stores = {p: s for p, s in idx.stores.items() if not projects or p in projects}
    linked = link_filter["mode"] if link_filter else None
    rows = []
    for pid, st in stores.items():
        f = _source_filter(sources, pid)
        if f == []:
            continue
        if link_filter:
            st.set_link_filter(_link_pairs(idx.cfg, pid, link_filter["nodes"]))
        cond, args = chunk_filter(f, open_only, kinds, linked, where)
        # filter first (through the date and author indexes), then one row per document: grouped directly, SQLite scans every chunk in document order
        sql = (f"WITH m AS MATERIALIZED (SELECT c.rowid r, c.source s, c.doc doc, COALESCE({DATE_SQL[field]}, '') d FROM chunks c WHERE 1 {cond}) "
               f"SELECT r, max(d) d FROM m GROUP BY s, doc ORDER BY d DESC LIMIT ?")
        rows += [(d, pid, rid) for rid, d in st.db.execute(sql, (*args, k))]
    rows.sort(key=lambda r: r[0], reverse=True)
    return [(pid, rid) for _, pid, rid in rows[:k]]


def search(idx, q, k=8, projects=None, sources=None, mode="hybrid", open_only=False, embedder=None, reranker=None, pool_docs=30, kinds=None,
           fusion=None, blend=BLEND, explain=False, link_filter=None, refs_in_query=True, link_boost=None, where=None, sort="relevance"):
    """Ranked [(project, rowid)] plus per-hit detail {(project, rowid): {"bm25": rank, "vec": rank, "rerank": score}} (ranks are 1-based).
    `fusion` ({k, top_bonus}) tunes the reciprocal rank fusion. Reranked hits are ordered by a position-aware blend of the fused score
    (scaled so the best is 1) and the reranker's P(relevant): the weight of the fused score is `blend_weight(fused rank)`, 75% for the top three,
    so a good retrieval order is not thrown away by a cross-encoder that is wrong about one hit; `blend=None` orders by the reranker alone.
    `explain` adds the arithmetic to each hit's detail as "explain". `where` (store.check_where) restricts by date and author. An empty query lists what the
    filters match, newest first (`browse`); `sort` "recent" orders the best `k` by date instead of relevance (newest first)."""
    if not q.strip():
        return browse(idx, k, projects, sources, open_only, kinds, link_filter, where), {}
    fusion = {**FUSION, **(fusion or {})}
    pool = max(50, k * 5)
    stores = {p: s for p, s in idx.stores.items() if not projects or p in projects}
    linked = link_filter["mode"] if link_filter else None
    for pid, st in stores.items():
        if link_filter:
            st.set_link_filter(_link_pairs(idx.cfg, pid, link_filter["nodes"]))
    rankings, names = [], []
    if mode in ("hybrid", "bm25"):
        hits = []
        for pid, st in stores.items():
            f = _source_filter(sources, pid)
            if f == []:
                continue
            hits += [(score, pid, rid) for rid, score in bm25(st, q, pool, f, open_only, kinds, linked, where)]
        rankings.append([(pid, rid) for _, pid, rid in sorted(hits)[:pool]]), names.append("bm25")
    if mode in ("hybrid", "vec") and embedder is not None:
        from embed import vector_search
        qvec, hits = embedder.query(q), []
        for pid, st in stores.items():
            f = _source_filter(sources, pid)
            if f == []:
                continue
            hits += [(-cos, pid, rid) for rid, cos in vector_search(st, qvec, embedder.name, pool, f, open_only, kinds, linked, where)]
        rankings.append([(pid, rid) for _, pid, rid in sorted(hits)[:pool]]), names.append("vec")
    named = idx.links.names_in_text(q) if refs_in_query and idx.links else []
    if named:                                  # the query spells out a reference (`scala/bug#1234`, `#123`): those documents and what links to them come first
        cond, args = chunk_filter(None, open_only, kinds, linked, where)
        pins = _pinned(idx, stores, [n for n in named if n["indexed"]], cond, args)
        if sources:
            pins = [key for key in pins if _source_ok(idx, stores, key, sources)]
        if pins:
            rankings.append(pins), names.append("pin")
    detail = {}
    for name, r in zip(names, rankings):
        for i, key in enumerate(r):
            detail.setdefault(key, {})[name] = i + 1
    doc_of = {}
    def doc(key):
        if key not in doc_of:
            pid, rid = key
            doc_of[key] = (pid, *stores[pid].db.execute("SELECT source, doc FROM chunks WHERE rowid=?", (rid,)).fetchone())
        return doc_of[key]
    fused = fuse_scores(rankings, fusion["k"], fusion["top_bonus"])
    seen, out = set(), []                      # one hit per document: the best chunk of an issue, file or page
    for key in sorted(fused, key=lambda key: fused[key][0], reverse=True):
        if doc(key) not in seen:
            seen.add(doc(key))
            out.append(key)
    rel_cfg = idx.cfg.search["related"]
    boost = rel_cfg["boost"] if link_boost is None else link_boost
    if boost and idx.links and out:            # an experiment, off by default: a candidate linked to the top few gets up to `boost` x the best score on top
        seeds = {n: 1 / (i + 1) for i, n in enumerate(node_of(idx, key) for key in out[:rel_cfg["seeds"]]) if n}
        rel = {e["node"]["id"]: e["score"] for e in idx.links.expand(seeds, rel_cfg["weights"], rel_cfg["hub_degree"], rel_cfg["depth2"])} if seeds else {}
        if rel:
            best, top0 = max(rel.values()), fused[out[0]][0]
            for key in out:
                n = node_of(idx, key)
                if n in rel:
                    fused[key] = (fused[key][0] + boost * top0 * rel[n] / best, fused[key][1])
                    detail.setdefault(key, {})["link_boost"] = round(boost * top0 * rel[n] / best, 4)
            out.sort(key=lambda key: fused[key][0], reverse=True)
    top = fused[out[0]][0] if out else 1.0
    pos = {key: i + 1 for i, key in enumerate(out)}
    if explain:
        for key in out:
            detail.setdefault(key, {})["explain"] = {"fused_rank": pos[key], "rrf": round(fused[key][0], 4), "top_bonus": fusion["top_bonus"] and round(fused[key][1], 4),
                                                     "retrieval": round(fused[key][0] / top, 4)}
    if reranker is not None and out:
        cand = out[:pool_docs]
        for pid in stores:                     # a small project's best hit must reach the reranker even when big projects crowd the top
            best = next((key for key in out if key[0] == pid), None)
            if best is not None and best not in cand:
                cand.append(best)
        docs = [" ".join(stores[pid].db.execute("SELECT title, text FROM chunks WHERE rowid=?", (rid,)).fetchone()) for pid, rid in cand]
        sc = reranker.scores(q, docs)
        final = []
        for key, x in zip(cand, sc):
            detail[key]["rerank"] = round(x, 4)
            w = blend_weight(pos[key], blend) if blend else 0.0
            final.append(w * fused[key][0] / top + (1 - w) * x)
            if explain:
                detail[key]["explain"].update({"weight": w, "final": round(final[-1], 4)})
        out = [key for key, _ in sorted(zip(cand, final), key=lambda x: -x[1])]
    out = out[:k]
    if sort == "recent":
        field = (where or {}).get("date", "created")
        out.sort(key=lambda key: _date_of(idx, key, field), reverse=True)
    return out, detail


_TITLE_AUTHOR = re.compile(r"\((?:comment|review comment on .+?) by ([A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?)\)\s*$")


def _who_and_when(db, sid, title, m):
    """Author (GitHub login), git author name (commits), created and updated times, and for comments and review comments the thread they belong to.
    Chunks written before a field was captured fall back: a comment's author from its title, a commit's `author` was the git name, a release's
    time from its tag date."""
    kind = m.get("kind") or "file"
    author, name = m.get("author"), m.get("author_name")
    if kind == "commit" and name is None:
        author, name = None, author                                      # written before the GitHub handle was captured
    if not author and kind in ("comment", "review"):
        author = (_TITLE_AUTHOR.search(title) or [None, None])[1]
    created = m.get("created") or (m.get("updated") if kind == "commit" else None) or (m.get("published") if kind == "release" else None)
    updated = m.get("updated") or (m.get("published") if kind == "release" else None)
    thread = None
    if kind in ("comment", "review") and m.get("number") is not None:
        r = db.execute("SELECT json_extract(meta, '$.author'), json_extract(meta, '$.created'), json_extract(meta, '$.kind') FROM chunks WHERE id = ?",
                       (f"{sid}:issue:{m['number']}",)).fetchone()
        if r:
            thread = {"number": m["number"], "kind": r[2], "author": r[0], "created": r[1]}
    return {"kind": kind, "author": author, "author_name": name, "created": created, "updated": updated, "thread": thread}


def _day(t):
    return (t or "")[:10]


def summary_line(h):
    """One line a client can show as is: [state] kind title, who and when, source, link."""
    kind = h["kind"] + (f" on {h['thread']['kind']} #{h['thread']['number']}" if h.get("thread") else "")
    who = h["author"] and f"@{h['author']}" or h.get("author_name")
    when = _day(h["created"]) + (f" (updated {_day(h['updated'])})" if h["updated"] and _day(h["updated"]) != _day(h["created"]) else "") if h["created"] else _day(h["updated"])
    flag = f"[{h['state']}] " if h["state"] in ("open", "closed", "merged") else ""
    return f"{flag}{h['title']} · {kind}" + "".join(f" · {x}" for x in (who, when, h["key"]) if x) + f" · {h['url']}"


def hits(idx, q, text_chars=1200, **kw):
    """`search` as plain dicts: ref (`project/chunk id`, what the `get` tool takes), project, source, label and colour (from config), title, url, state, text (the chunk, cut), who and when (author handle,
    created and updated times, the thread of a comment), the ranks and scores, and `links` (what it is linked to, see LINKS.md) when there is a links database."""
    keys, detail = search(idx, q, **kw)
    out, nodes = [], []
    for pid, rid in keys:
        db = idx.stores[pid].db
        t, text, url, meta, sid, doc, state, cid = db.execute(
            f"SELECT title, text, url, meta, source, doc, {STATE_SQL}, id FROM chunks c WHERE rowid=?", (rid,)).fetchone()
        m = json.loads(meta)
        src = idx.source(pid, sid)
        out.append({"ref": f"{pid}/{cid}", "project": pid, "source": sid, "key": src.key, "label": src.label, "color": src.color, "title": " ".join(t.split()), "url": url,
                    "doc": doc, "state": state, "labels": m.get("labels"), "text": text[:text_chars], "truncated": len(text) > text_chars,
                    **_who_and_when(db, sid, t, m), **detail.get((pid, rid), {})})
        out[-1]["line"] = summary_line(out[-1])
        nid = node_id(src.repo, out[-1]["kind"], m, doc)
        if nid:
            nodes.append((len(out) - 1, nid))
            out[-1]["node"] = nid
    if idx.links and nodes:                                              # what each hit is linked to: counts per relation, the few that matter most, a one-line form
        sums = idx.links.summaries([n for _, n in nodes])
        for i, nid in nodes:
            if nid in sums:
                out[i]["links"] = sums[nid]
                if sums[nid]["line"]:
                    out[i]["line"] += f" · {sums[nid]['line']}"
    return out


def related(idx, results, k=None, kinds=None, projects=None, open_only=False, weights=None, explain=False, where=None):
    """Documents linked to the top hits, for a group of their own under the results (LINKS.md): the first `related.seeds` hits seed an expansion over the links
    (`LinkDB.expand`), what the hits already show is left out, `kinds` / `projects` / `open_only` apply, and `related.per_kind` keeps one kind of thing (the
    commits of a big PR) from filling the group; `where` (dates, authors) applies to each related document as to hits. [{ref, node, kind, project, source, label, color, key, title, url, state, created, text, score, via, line}]."""
    cfg = idx.cfg.search["related"]
    k = cfg["limit"] if k is None else k
    if not (idx.links and cfg["enabled"] and k):
        return []
    seeds = {h["node"]: 1 / (i + 1) for i, h in enumerate(results[:cfg["seeds"]]) if h.get("node")}
    if not seeds:
        return []
    found = idx.links.expand(seeds, {**cfg["weights"], **(weights or {})}, cfg["hub_degree"], cfg["depth2"], exclude={h["node"] for h in results if h.get("node")})
    out, per = [], {}
    wcond, wargs = chunk_filter(where=where)
    for e in found:
        n = e["node"]
        if (projects and n["project"] not in projects) or (open_only and n["state"] in ("closed", "merged")) or n["project"] not in idx.stores or not n["chunk"]:
            continue
        row = idx.stores[n["project"]].db.execute(f"SELECT source, title, text, meta FROM chunks c WHERE id = ? {wcond}", (n["chunk"], *wargs)).fetchone()
        if row is None:
            continue
        sid, title, text, meta = row
        m = json.loads(meta)
        kind = m.get("kind") or "file"
        if (kinds and kind not in kinds) or per.get(kind, 0) >= cfg["per_kind"]:
            continue
        per[kind] = per.get(kind, 0) + 1
        src = idx.source(n["project"], sid)
        item = {"ref": n["get_ref"], "node": n["id"], "kind": kind, "project": n["project"], "source": sid, "key": src.key, "label": src.label, "color": src.color,
                "title": " ".join(title.split()), "url": n["url"], "state": n["state"], "created": n["created"], "text": text[:300], "score": round(e["score"], 4),
                "via": [{k2: v for k2, v in x.items() if k2 != "score"} for x in e["via"][:3]]}
        item["line"] = " · ".join(f"{SAY[INV[x['rel']]]} {x['from']}" + (f" (through {x['through']})" if x.get("through") else "") for x in item["via"][:2])
        if explain:
            item["explain"] = e["via"]
        out.append(item)
        if len(out) >= k:
            break
    return out


def tuning(cfg):
    """The search settings of search.json as keyword arguments of `search` / `hits`."""
    r = cfg.search["reranker"]
    return {"pool_docs": r["candidates"], "blend": r.get("blend", BLEND), "fusion": cfg.search.get("fusion")}


def show(rows, width=240):
    for n, h in enumerate(rows, 1):
        flag = f" [{h['state']}]" if h["state"] in ("closed", "merged") else ""
        snippet = " ".join(h["text"].split())[:width]
        x = h.get("explain")
        why = (f"   fused #{x['fused_rank']} rrf {x['rrf']} (top bonus {x['top_bonus']}) keyword #{h.get('bm25', '-')} vector #{h.get('vec', '-')}"
               + (f" | rerank {h['rerank']} x {1 - x['weight']:.2f} + retrieval {x['retrieval']} x {x['weight']:.2f} = {x['final']}" if "weight" in x else "") + "\n") if x else ""
        print(f"{n}. [{h['key']}]{flag} {h['title']}\n   {h['url']}\n{why}   {snippet}\n")


if __name__ == "__main__":
    a = sys.argv[1:]
    def opt(name, default=None, many=False):
        vals = []
        while name in a:
            i = a.index(name); vals.append(a[i + 1]); del a[i:i + 2]
        return vals if many else (vals[-1] if vals else default)
    cfg = config.load()
    k, uni, sort = int(opt("-k", 8)), opt("--universe"), opt("--sort", "relevance")
    where = check_where({"since": opt("--since"), "until": opt("--until"), "date": opt("--date"), "authors": opt("--author", many=True)}, cfg.search["me"])
    projects, sources, kinds = opt("--project", many=True), opt("--source", many=True), opt("--kind", many=True)
    mode = "bm25" if "--bm25" in a else "vec" if "--vec" in a else "hybrid"
    q = " ".join(x for x in a if not x.startswith("--"))
    idx = Index(cfg, uni)
    if idx.missing:
        print(f"(not indexed yet: {', '.join(idx.missing)})", file=sys.stderr)
    emb = rr = None
    if mode != "bm25":
        try:
            from embed import load
            emb = load(cfg, "local" if "--local" in a else None)
        except Exception as e:
            print(f"(no embedder: {e}; keyword only)", file=sys.stderr)
    if "--rerank" in a:
        from rerank import Reranker
        rr = Reranker(cfg.search["reranker"]["model"])
    show(hits(idx, q, k=k, projects=projects, sources=sources, mode=mode, open_only="--open" in a, kinds=kinds, embedder=emb, reranker=rr,
              explain="--explain" in a, where=where, sort=sort, **tuning(cfg)))
