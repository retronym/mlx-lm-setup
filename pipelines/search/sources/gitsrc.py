"""Git-backed sources: scala/scala code and spec, the Scala 3 docs. Change detection is the blob sha of each file (git computes
it for free), so a sync lists the tree, re-chunks only files whose blob changed, and deletes files that disappeared. Renames cost
nothing extra: the new path has the same blob but no stored state, so it is re-chunked once and the old path is deleted."""
import re, subprocess
from store import Chunk

CHUNKER_VERSION = "1"        # bump to re-chunk everything
MAX = 2400                   # chars per chunk, before the context header

_DEF = re.compile(r"^(\s{0,4})(?:(?:private|protected|final|override|abstract|sealed|implicit|lazy|case|inline|transparent|open|opaque|given|@\w+(?:\([^)]*\))?)\s+)*"
                  r"(def|class|object|trait|enum|given|extension|type|val|var)\s+`?([A-Za-z_][\w$]*|\[)")
_PKG = re.compile(r"^package\s+([\w.]+)")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def tree(repo, ref, prefixes, suffixes):
    out = git(repo, "ls-tree", "-r", ref, "--", *prefixes).decode()
    files = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        if path.endswith(suffixes):
            files[path] = meta.split()[2]
    return files


def _split_big(lines, limit):
    """Split an over-long block at blank lines (hard split if a stretch has none)."""
    out, cur, n = [], [], 0
    for ln in lines:
        if n > limit and (not ln.strip() or n > limit * 1.5):
            out.append(cur)
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        out.append(cur)
    return out


def chunk_scala(path, text):
    """One chunk per member-level definition (indent <= 4), each prefixed with the file path, package and the enclosing
    definition's name; leading comments stay with the definition they document."""
    lines = text.splitlines()
    pkg = next((m.group(1) for ln in lines[:60] if (m := _PKG.match(ln))), "")
    starts = []                       # (line index, name, indent)
    for i, ln in enumerate(lines):
        m = _DEF.match(ln)
        if m and m.group(3) != "[":
            j = i
            while j > 0 and (lines[j - 1].lstrip().startswith(("*", "/**", "/*", "//", "@")) or lines[j - 1].strip().endswith("*/")):
                j -= 1
            if not starts or j >= starts[-1][0] + 1:
                starts.append((j, m.group(3), len(m.group(1))))
    if not starts:
        starts = [(0, path.rsplit("/", 1)[-1], 0)]
    elif starts[0][0] > 0:
        starts.insert(0, (0, "(header)", 0))
    chunks, seen, scope = [], {}, []
    for k, (s, name, indent) in enumerate(starts):
        e = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        while scope and scope[-1][1] >= indent:
            scope.pop()
        enclosing = ".".join(n for n, _ in scope)
        kind = next((m.group(2) for ln in lines[s:e] if (m := _DEF.match(ln))), "")
        if kind in ("class", "object", "trait", "enum"):
            scope.append((name, indent))
        for part in _split_big(lines[s:e], MAX):
            body = "\n".join(part).strip("\n")
            if len(body.strip()) < 20:
                continue
            n = seen[(enclosing, name)] = seen.get((enclosing, name), 0) + 1
            title = f"{path}  {pkg}  {enclosing + '.' if enclosing else ''}{name}"
            chunks.append((f"{name}#{n}", title, body, s + 1))
    return chunks


def chunk_markdown(path, text):
    """One chunk per heading section, prefixed with the heading trail; long sections split at blank lines."""
    chunks, trail, cur, start = [], [], [], 1
    def flush(end_line):
        body = "\n".join(cur).strip()
        if len(body) >= 40:
            for n, part in enumerate(_split_big(body.splitlines(), MAX)):
                chunks.append((f"{' > '.join(trail) or '(top)'}#{n}", f"{path}  {' > '.join(trail)}", "\n".join(part), start))
    in_code = False
    for i, ln in enumerate(text.splitlines(), 1):
        if ln.startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            flush(i)
            cur.clear()
            start = i
            trail[:] = trail[:len(m.group(1)) - 1] + [m.group(2).strip()]
        cur.append(ln)
    flush(len(text.splitlines()))
    return chunks


class GitSource:
    def __init__(self, name, repo, web, ref, prefixes, suffixes, chunker):
        self.name, self.repo, self.web, self.ref, self.prefixes, self.suffixes, self.chunker = name, repo, web, ref, prefixes, suffixes, chunker

    def sync(self, store, limit=None, log=print):
        head = git(self.repo, "rev-parse", self.ref).decode().strip()
        files = tree(self.repo, self.ref, self.prefixes, self.suffixes)
        known = {r[0]: r[1] for r in store.db.execute("SELECT k, v FROM state WHERE source=? AND k LIKE 'file:%'", (self.name,))}
        known = {k[5:]: v for k, v in known.items()}
        todo = [p for p, b in files.items() if known.get(p) != f"{CHUNKER_VERSION}:{b}"]
        gone = [p for p in known if p not in files]
        tot = [0, 0, 0, 0]
        for p in gone:
            tot[2] += store.delete_doc(self.name, p)
            store.db.execute("DELETE FROM state WHERE source=? AND k=?", (self.name, "file:" + p))
        for n, p in enumerate(todo[:limit]):
            text = git(self.repo, "show", f"{self.ref}:{p}").decode(errors="replace")
            seen, chunks = {}, []
            for cid, title, body, line in self.chunker(p, text):
                n = seen[cid] = seen.get(cid, 0) + 1          # ids stay unique within a file; stable while the file's outline is
                chunks.append(Chunk(f"{self.name}:{p}:{cid}" + (f"~{n}" if n > 1 else ""), p, title, body,
                                    f"{self.web}/blob/{head}/{p}#L{line}", {"line": line}))
            for i, v in enumerate(store.apply(self.name, chunks, doc=p)):
                tot[i] += v
            store.put(self.name, "file:" + p, f"{CHUNKER_VERSION}:{files[p]}")
            if n % 200 == 199:
                store.commit()
                log(f"  {self.name}: {n + 1}/{len(todo)} files")
        store.put(self.name, "head", head)
        store.commit()
        log(f"{self.name} @ {head[:10]}: {len(files)} files, {len(todo[:limit])} re-chunked, {len(gone)} removed -> +{tot[0]} ~{tot[1]} -{tot[2]} ={tot[3]} chunks")


import os
CODE = os.path.expanduser("~/code")
SOURCES = {
    "scalac": lambda: GitSource("scalac", f"{CODE}/scala/scala", "https://github.com/scala/scala", "HEAD",
                                ["src/compiler", "src/reflect", "src/library", "spec"], (".scala", ".md"),
                                lambda p, t: chunk_markdown(p, t) if p.endswith(".md") else chunk_scala(p, t)),
    "scala3docs": lambda: GitSource("scala3docs", f"{CODE}/scala/scala3", "https://github.com/scala/scala3", "HEAD",
                                    ["docs/_docs/reference", "docs/_docs/internals"], (".md",), chunk_markdown),
}
