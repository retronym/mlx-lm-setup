"""Reading the links database (`links.py` writes it): what the search backend and the gateway use. Standard library only, so the gateway can load it
without the indexer's dependencies. See LINKS.md.

A link has a *relation name* seen from one end: an edge `A -closes-> B` is `closes` for A and `closed_by` for B. `NAMES` lists them all; `has_link`
filters and the `links` tool use the names (for both ends of one edge type, name both: `closes` and `closed_by`)."""
import os, re, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refs import make_parser

NAMES = {"closes": ("closes", "out"), "closed_by": ("closes", "in"), "merged_as": ("merged_as", "out"), "merge_of": ("merged_as", "in"),
         "mentions": ("mentions", "out"), "mentioned_by": ("mentions", "in"), "shipped_in": ("shipped_in", "out"), "ships": ("shipped_in", "in"),
         "touches": ("touches", "out"), "touched_by": ("touches", "in"), "defines": ("defines", "out"), "defined_by": ("defines", "in")}
REL = {v: k for k, v in NAMES.items()}
TYPES = sorted({t for t, _ in NAMES.values()})
TOP_ORDER = ["closes", "closed_by", "merged_as", "merge_of", "shipped_in", "mentions", "mentioned_by", "defines", "defined_by"]     # what a hit's short list shows (not file touches, not a release's whole content)
SAY = {"closes": "closes", "closed_by": "closed by", "merged_as": "merged as", "merge_of": "merge of", "shipped_in": "shipped in", "mentions": "mentions",
       "mentioned_by": "mentioned by", "defines": "referenced in code", "defined_by": "referenced by code"}


def node_id(repo, kind, meta, doc):
    """The links node a chunk belongs to, or None: `owner/repo#N` for an issue, PR, comment or review, `commit:owner/repo@sha`, `release:owner/repo@tag`,
    `file:owner/repo:path`. `kind` is the chunk's kind ("file" for a chunk of a git tree), `meta` its metadata, `doc` its document."""
    if kind in ("issue", "pr", "comment", "review"):
        return f"{repo}#{meta['number']}" if meta.get("number") is not None else None
    if kind == "commit":
        return f"commit:{repo}@{meta['sha']}" if meta.get("sha") else None
    if kind in ("release", "tag"):
        return f"release:{repo}@{meta['tag']}" if meta.get("tag") else None
    if kind == "file":
        return f"file:{repo}:{doc}"
    return None


def doc_names(kind, ref):
    """The `doc` values chunks of a node carry (a release or tag node: both spellings)."""
    return {"issue": [f"issue:{ref}"], "pr": [f"issue:{ref}"], "commit": [f"commit:{ref}"], "release": [f"release:{ref}", f"tag:{ref}"], "file": [ref]}.get(kind, [])


def short(n):
    """A short label for a node row dict: `o/r#12`, `commit abcdef12`, `release v1.0`, a file's path."""
    k = n["kind"]
    return {"commit": f"commit {(n['ref'] or '')[:8]}", "release": f"release {n['ref']}", "file": f"file {n['ref']}"}.get(k, n["id"])


class LinkDB:
    """Read-only access to `data/links/<universe>.db`; `LinkDB.open` returns None when there is none (links are optional everywhere)."""

    COLS = "id, kind, project, repo, ref, title, state, created, url, indexed, chunk"

    def __init__(self, cfg, path):
        self.cfg = cfg
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        self.alias = cfg.search["links"]["repo_aliases"]
        self.parse = make_parser(cfg.search["links"]["legacy_prefixes"])

    @classmethod
    def open(cls, cfg, universe_id=None):
        path = cfg.data_path("links", f"{cfg.universe(universe_id).id}.db")
        try:
            return cls(cfg, path) if path.exists() else None
        except sqlite3.Error:
            return None

    def close(self):
        self.db.close()

    def _node(self, row):
        n = dict(zip(self.COLS.split(", "), row))
        n["indexed"] = bool(n["indexed"])
        n["get_ref"] = f"{n['project']}/{n['chunk']}" if n["indexed"] and n["project"] and n["chunk"] else None
        return n

    def node(self, nid):
        r = self.db.execute(f"SELECT {self.COLS} FROM nodes WHERE id = ?", (nid,)).fetchone()
        return self._node(r) if r else None

    # ---- naming a thing --------------------------------------------------------------------------------------------------------
    def resolve(self, text):
        """Node dicts for what `text` names: a node id (`scala/bug#1`, `commit:o/r@sha`), a hit's `ref` (`project/chunk id`), `#123` (every indexed issue or PR of
        that number), a legacy id (`SI-123`), a commit url or a 7+ digit sha. Empty when it names nothing the links know."""
        t = (text or "").strip()
        if not t:
            return []
        n = self.node(t)
        if n:
            return [n]
        if "/" in t:
            pid, cid = t.split("/", 1)
            r = self.db.execute(f"SELECT {self.COLS} FROM nodes WHERE project = ? AND chunk = ?", (pid, cid)).fetchone()
            if r:
                return [self._node(r)]
        if re.fullmatch(r"[0-9a-f]{7,40}", t):
            return [self._node(r) for r in self.db.execute(f"SELECT {self.COLS} FROM nodes WHERE kind = 'commit' AND ref LIKE ? AND indexed = 1 LIMIT 5", (t + "%",))]
        out = []
        for ref in self.parse(t):
            if ref.kind == "issue" and ref.repo:
                n = self.node(f"{self.alias.get(ref.repo, ref.repo)}#{ref.key}")
                out += [n] if n else []
            elif ref.kind == "issue":
                out += [self._node(r) for r in self.db.execute(f"SELECT {self.COLS} FROM nodes WHERE ref = ? AND kind IN ('issue', 'pr') AND indexed = 1 ORDER BY repo", (ref.key,))]
            elif ref.repo:
                out += [self._node(r) for r in self.db.execute(f"SELECT {self.COLS} FROM nodes WHERE kind = 'commit' AND repo = ? AND ref LIKE ?", (self.alias.get(ref.repo, ref.repo), ref.key + "%"))]
            else:
                out += [self._node(r) for r in self.db.execute(f"SELECT {self.COLS} FROM nodes WHERE kind = 'commit' AND ref LIKE ? AND indexed = 1 LIMIT 5", (ref.key + "%",))]
        return list({n["id"]: n for n in out}.values())

    def names_in_text(self, text):
        """Nodes a *query* names by an explicit reference (`scala/bug#1234`, a URL, `SI-1234`, a bare `#123`): what the search puts first."""
        return [n for ref in self.parse(text or "") for n in self.resolve(_spell(ref))]

    # ---- relations -------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _wanted(types):
        """{(edge type, 'out'|'in')} for relation names (`closes` is the outgoing end, `closed_by` the incoming one: name both for either direction); None = all."""
        if not types:
            return None
        bad = [t for t in types if t not in NAMES]
        if bad:
            raise ValueError(f"unknown link relation {bad[0]!r} (have: {', '.join(NAMES)})")
        return {NAMES[t] for t in types}

    def neighbours(self, nid, types=None, limit=50):
        """[{rel, conf, how, snip, node}] around `nid`, both directions, most important first (the order of TOP_ORDER, then confidence); `types` filters."""
        want, rows = self._wanted(types), []
        for direction, mine, other in (("out", "src", "dst"), ("in", "dst", "src")):
            for t, conf, how, snip, *n in self.db.execute(
                    f"SELECT e.type, e.conf, e.how, e.snip, {', '.join('n.' + c for c in self.COLS.split(', '))} FROM edges e JOIN nodes n ON n.id = e.{other} WHERE e.{mine} = ? "
                    f"ORDER BY e.conf DESC LIMIT 2000", (nid,)):
                if want is None or (t, direction) in want:
                    rows.append({"rel": REL[(t, direction)], "conf": conf, "how": how, "snip": snip, "node": self._node(n)})
        pos = {r: i for i, r in enumerate(TOP_ORDER + [r for r in NAMES if r not in TOP_ORDER])}
        rows.sort(key=lambda r: (pos[r["rel"]], -r["conf"], not r["node"]["indexed"], r["node"]["created"] or ""))
        return rows[:limit]

    def summaries(self, nids):
        """{node id: {"counts": {relation: n}, "top": [{rel, id, kind, title, state, indexed, get_ref, conf}]}} for the nodes that have any link."""
        out = {}
        for nid in set(nids):
            counts = {}
            for direction, col in (("out", "src"), ("in", "dst")):
                for t, n in self.db.execute(f"SELECT type, count(*) FROM edges WHERE {col} = ? GROUP BY type", (nid,)):
                    counts[REL[(t, direction)]] = n
            if not counts:
                continue
            top, per = [], {}
            shown = [x for x in TOP_ORDER if x in counts]
            for r in (self.neighbours(nid, shown, 40) if shown else []):
                if per.get(r["rel"], 0) >= 2 or len(top) >= 6:
                    continue
                per[r["rel"]] = per.get(r["rel"], 0) + 1
                n = r["node"]
                top.append({"rel": r["rel"], "id": n["id"], "kind": n["kind"], "title": n["title"], "state": n["state"], "indexed": n["indexed"], "get_ref": n["get_ref"], "url": n["url"], "conf": r["conf"]})
            out[nid] = {"counts": counts, "top": top, "line": line(counts, top)}
        return out

    # ---- filters ---------------------------------------------------------------------------------------------------------------
    def nodes_with(self, name):
        """{id: (kind, repo, ref)} of the indexed nodes that have a relation (`closed_by`: something closes it), for the `has_link` filter."""
        if name not in NAMES:
            raise ValueError(f"unknown link relation {name!r} (have: {', '.join(NAMES)})")
        t, d = NAMES[name]
        col = "src" if d == "out" else "dst"
        return {r[0]: r[1:] for r in self.db.execute(f"SELECT DISTINCT n.id, n.kind, n.repo, n.ref FROM edges e JOIN nodes n ON n.id = e.{col} WHERE e.type = ? AND n.indexed = 1", (t,))}

    def linked_to(self, nids, types=None):
        """{id: (kind, repo, ref)} of the indexed nodes with an edge to or from any of `nids` (not `nids` themselves), optionally only through `types`."""
        out = {}
        for nid in nids:
            for r in self.neighbours(nid, types, 100000):
                n = r["node"]
                if n["indexed"] and n["id"] not in nids:
                    out[n["id"]] = (n["kind"], n["repo"], n["ref"])
        return out

    def story(self, nid, limit=60):
        """The documents around `nid` in time order: depth 1 through every relation but file touches, depth 2 only through closes / merged_as / shipped_in
        (an issue's fixing PR's merge commit and release), skipping hubs. [{when, node, via: {from, rel}}]."""
        seen, order, frontier = {nid: None}, [], [nid]
        for depth in (1, 2):
            nxt = []
            for cur in frontier:
                degree = self.db.execute("SELECT (SELECT count(*) FROM edges WHERE src = ?) + (SELECT count(*) FROM edges WHERE dst = ?)", (cur, cur)).fetchone()[0]
                if depth == 2 and degree > 200:
                    continue
                for r in self.neighbours(cur, [x for x in NAMES if x not in ("touches", "touched_by", "ships")] if depth == 1 else ["closes", "closed_by", "merged_as", "merge_of", "shipped_in"], 60):
                    n = r["node"]
                    if n["id"] in seen or (r["rel"] in ("mentions", "mentioned_by") and r["conf"] < 0.5):
                        continue
                    seen[n["id"]] = {"from": cur, "rel": r["rel"]}
                    order.append(n)
                    nxt.append(n["id"])
            frontier = nxt
        order = order[:limit]
        order.append(self.node(nid))
        order.sort(key=lambda n: (n["created"] or "9999", n["id"]))
        return [{"when": n["created"], "node": n, "via": seen[n["id"]]} for n in order if n]


def _spell(ref):
    """A reference as text `resolve` understands."""
    if ref.kind == "commit":
        return f"https://github.com/{ref.repo}/commit/{ref.key}" if ref.repo else ref.key
    return f"{ref.repo}#{ref.key}" if ref.repo else f"#{ref.key}"


def line(counts, top):
    """`closes o/r#1 · closed by o/r#2 · shipped in release v1.0 · 5 mentions`: the one-line form of a hit's links."""
    parts = [f"{SAY[t['rel']]} {short({'kind': t['kind'], 'id': t['id'], 'ref': t['id'].split('@')[-1] if t['kind'] in ('commit', 'release') else None})}" for t in top[:3]
             if t["rel"] not in ("mentions", "mentioned_by")]
    m = counts.get("mentions", 0), counts.get("mentioned_by", 0)
    if m[0]:
        parts.append(f"mentions {m[0]}")
    if m[1]:
        parts.append(f"mentioned by {m[1]}")
    return " · ".join(parts)
