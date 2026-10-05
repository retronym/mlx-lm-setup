"""Read whole documents out of the search index: the `get` tool. A search hit is a passage; this returns what it belongs to, read straight from
the SQLite files (and, for a file, from the managed bare clone), so it starts no model and no backend.

A `ref` is `<project>/<chunk id>` (every search hit carries one). Scopes: `chunk` is the chunk itself; `doc` (default) is every chunk of its
document in reading order (an issue or PR with all its comments and review comments, a markdown file by section, a commit, a release);
`file` is, for a hit in a git tree, the file itself at the indexed commit, optionally a `lines` range ("120-180")."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess

MAX_REFS = 20
_BLOB = re.compile(r"/blob/([0-9a-f]{7,40})/([^#]+)")


def _row(con, cid):
    return con.execute("SELECT rowid, id, source, doc, title, text, url, meta FROM chunks WHERE id = ?", (cid,)).fetchone()


def _file_text(cfg, repos, project, source_id, url, lines):
    """(text, line range, total lines) of the file a `file` chunk points at, from the managed clone, or None when it cannot be read."""
    m = _BLOB.search(url or "")
    if not m:
        return None
    try:
        repo = cfg.projects[project].source(source_id).repo
        out = subprocess.run(["git", "--git-dir", str(repos.path_for(cfg, repo)), "show", f"{m.group(1)}:{m.group(2)}"], capture_output=True, text=True, timeout=30, check=True).stdout
    except (KeyError, AttributeError, OSError, subprocess.SubprocessError):
        return None
    rows = out.split("\n")
    a, b = 1, len(rows)
    if lines:
        x = re.fullmatch(r"(\d+)(?:-(\d+))?", lines.strip())
        if not x:
            raise ValueError(f"lines must look like 120 or 120-180, not {lines!r}")
        a = max(1, int(x.group(1))); b = min(len(rows), int(x.group(2) or a))
        if b < a:
            raise ValueError("lines: the end is before the start")
    return "\n".join(rows[a - 1:b]), [a, b], len(rows)


def _page(text, offset, max_chars):
    return text[offset:offset + max_chars], offset + max_chars < len(text)


def get(cfg, refs: list[str], *, scope: str = "doc", max_chars: int = 6000, offset: int = 0, lines: str | None = None, repos=None) -> dict:
    if scope not in ("chunk", "doc", "file"):
        raise ValueError("scope must be chunk, doc or file")
    if not refs or len(refs) > MAX_REFS or not all(isinstance(r, str) and "/" in r for r in refs):
        raise ValueError(f"refs must be 1..{MAX_REFS} strings like 'zinc/issues:issue:100' (the `ref` of a search hit)")
    if not 100 <= max_chars <= 50000 or offset < 0:
        raise ValueError("max_chars must be 100..50000 and offset >= 0")
    cons, results = {}, []
    try:
        for ref in refs:
            pid, cid = ref.split("/", 1)
            res = {"ref": ref, "found": False}
            results.append(res)
            if pid not in cfg.projects or not cfg.project_db(pid).exists():
                res["error"] = f"project {pid!r} is not indexed"
                continue
            if pid not in cons:
                cons[pid] = sqlite3.connect(f"file:{cfg.project_db(pid)}?mode=ro", uri=True, timeout=30)
            con = cons[pid]
            r = _row(con, cid)
            if r is None:
                res["error"] = "no such chunk (the index may have moved on: search again)"
                continue
            rid, _, sid, doc, title, text, url, meta = r
            m = json.loads(meta)
            kind = m.get("kind") or "file"
            src = cfg.projects[pid].source(sid)
            res.update(found=True, project=pid, source=sid, key=src.key, label=src.label, doc=doc, kind=kind, title=" ".join(title.split()), url=url,
                       state=m.get("state"), author=m.get("author"), created=m.get("created"), updated=m.get("updated"), scope=scope)
            body = None
            if scope == "file" and kind == "file":
                f = _file_text(cfg, repos, pid, sid, url, lines) if repos else None
                if f:
                    body, res["lines"], res["total_lines"] = f[0], f[1], f[2]
                else:
                    res["note"] = "the file could not be read from the managed clone; returning the indexed chunks instead"
            elif scope == "file":
                res["note"] = f"scope=file only applies to files in a git tree, not to a {kind}; returning the document"
            if body is None:
                if scope == "chunk":
                    parts = [(title, text, m)]
                else:
                    parts = [(t, x, json.loads(mm)) for t, x, mm in con.execute(
                        "SELECT title, text, meta FROM chunks WHERE source = ? AND doc = ? ORDER BY COALESCE(json_extract(meta, '$.line'), 0), "
                        "COALESCE(json_extract(meta, '$.created'), ''), rowid", (sid, doc))]
                res["chunks"] = len(parts)
                if kind == "file":
                    body = "\n\n".join(x for _, x, _ in parts)
                else:                                                                 # a thread: say who wrote each chunk and when
                    body = "\n\n".join(f"### {' '.join(t.split())}" + (f" ({mm['created']})" if mm.get("created") else "") + f"\n{x}" for t, x, mm in parts)
            res["total_chars"] = len(body)
            res["offset"] = offset
            res["text"], res["truncated"] = _page(body, offset, max_chars)
            if res["truncated"]:
                res["next_offset"] = offset + max_chars
    finally:
        for c in cons.values():
            c.close()
    return {"results": results}
