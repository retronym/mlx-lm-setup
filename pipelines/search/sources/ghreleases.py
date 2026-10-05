"""GitHub release notes, plus annotated tag messages for tags that have no release. A release is a header chunk (name, date, the start of
the notes) and, for long notes, one chunk per heading section, all keyed by tag so an edited release re-embeds only what changed and a
deleted one disappears. Notes mention the issues and PRs they shipped (`#1234`); those numbers go into the chunk metadata (`refs`) so a
later feature can answer "which release shipped X". The whole list is a handful of requests, so every sync just re-reads it."""
import json, re, subprocess
from store import Chunk
from sources.ghissues import pages
from sources.gitsrc import chunk_markdown, _split_big

HEADER_CHARS = 800                        # notes up to this long are one chunk; longer ones are split by heading after a header chunk
_REF = re.compile(r"(?<![\w/])#(\d{1,6})\b|/(?:issues|pull)/(\d{1,6})\b")


def _refs(text):
    return sorted({int(a or b) for a, b in _REF.findall(text or "")})[:200]


def _release_chunks(src, r, max_chars):
    tag, body = r["tag_name"], (r.get("body") or "").strip()
    name = r.get("name") or tag
    when = (r.get("published_at") or r.get("created_at") or "")[:10]
    meta = {"kind": "release", "tag": tag, "published": when, "prerelease": bool(r.get("prerelease")), "refs": _refs(body),
            "state": "prerelease" if r.get("prerelease") else None}
    title = f"{src.repo} release {tag}" + (f" ({name})" if name != tag else "")
    doc, url = f"release:{tag}", r["html_url"]
    head = f"{title}\npublished {when}{' (pre-release)' if r.get('prerelease') else ''}\n\n{body[:HEADER_CHARS]}".strip()
    out = [Chunk(f"{src.id}:release:{tag}:header", doc, title, head, url, meta)]
    if len(body) > HEADER_CHARS:
        seen = {}
        for cid, sec_title, sec_body, line in chunk_markdown(f"{src.repo} release {tag}", body):
            n = seen[cid] = seen.get(cid, 0) + 1
            for k, part in enumerate(_split_big(sec_body.splitlines(), max_chars)):
                out.append(Chunk(f"{src.id}:release:{tag}:{cid}" + (f"~{n}" if n > 1 else "") + (f"/{k}" if k else ""), doc, f"{title}  {sec_title.split('  ', 1)[-1]}",
                                 "\n".join(part), url, {**meta, "refs": _refs(sec_body)}))
    return out


def _tag_chunks(src, repo_dir, skip_tags):
    """Annotated tags (those with a message) that no release covers."""
    fmt = "%(refname:short)%1f%(objecttype)%1f%(creatordate:short)%1f%(contents)%1e"
    raw = subprocess.run(["git", "-C", str(repo_dir), "for-each-ref", "refs/tags", f"--format={fmt}"], check=True, capture_output=True, text=True).stdout
    out = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        tag, otype, when, msg = rec.split("\x1f", 3)
        msg = msg.strip()
        if otype != "tag" or not msg or tag in skip_tags:
            continue
        title = f"{src.repo} tag {tag}"
        out.append(Chunk(f"{src.id}:tag:{tag}", f"tag:{tag}", title, f"{title}\ntagged {when}\n\n{msg}", f"https://github.com/{src.repo}/releases/tag/{tag}",
                         {"kind": "tag", "tag": tag, "published": when, "refs": _refs(msg)}))
    return out


class GhReleases:
    def __init__(self, src, repo_dir=None, max_chars=2400):
        self.src, self.name, self.repo_dir, self.max_chars = src, src.id, repo_dir, max_chars

    def sync(self, store, limit=None, since=None, log=print):
        releases = [r for p in pages(f"repos/{self.src.repo}/releases", log) for r in p if not r.get("draft")]
        by_doc = {}
        for r in releases:
            by_doc[f"release:{r['tag_name']}"] = _release_chunks(self.src, r, self.max_chars)
        if self.src.tag_messages and self.repo_dir:
            for ch in _tag_chunks(self.src, self.repo_dir, {r["tag_name"] for r in releases}):
                by_doc[ch.doc] = [ch]
        tot = [0, 0, 0, 0]
        for doc, chunks in by_doc.items():
            for i, v in enumerate(store.apply(self.name, chunks, doc=doc)):
                tot[i] += v
        for (doc,) in store.db.execute("SELECT DISTINCT doc FROM chunks WHERE source=?", (self.name,)).fetchall():
            if doc not in by_doc:
                tot[2] += store.delete_doc(self.name, doc)
        latest = max([(r.get("published_at") or r.get("created_at") or "")[:10] for r in releases] or [""])
        store.put(self.name, "last_release", latest)
        store.commit()
        log(f"{self.src.key}: {len(releases)} releases, {sum(1 for d in by_doc if d.startswith('tag:'))} tag messages -> +{tot[0]} ~{tot[1]} -{tot[2]} ={tot[3]} chunks")

    def reconcile(self, store, log=print):
        self.sync(store, log=log)                    # the full list is re-read on every sync, so a sync is a reconcile
