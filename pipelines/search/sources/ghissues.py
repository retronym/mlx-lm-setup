"""GitHub issues and their comments (scala/bug by default), via `gh api`. Incremental by `since=<updated_at cursor>`: the issues
endpoint and the repo-wide comments endpoint each return everything touched since the cursor in one paginated stream, so a sync
is a few requests. An issue is one chunk group (title + body), every comment is its own chunk keyed by comment id, so a new
comment embeds one new chunk and an edit re-embeds that comment only. State and labels are metadata (filterable at query time,
no re-embed). Deletions are invisible to `since`; `reconcile()` lists all live numbers and removes the rest."""
import json, subprocess
from store import Chunk
from sources.gitsrc import _split_big, MAX

DEFAULT_SINCE = "2023-01-01T00:00:00Z"        # backfill horizon for the PoC; --since overrides


def gh_pages(path, **params):
    q = "&".join(f"{k}={v}" for k, v in {"per_page": 100, **params}.items())
    out = subprocess.run(["gh", "api", "--paginate", f"{path}?{q}"], check=True, capture_output=True, text=True).stdout
    dec, i, items = json.JSONDecoder(), 0, []
    while i < len(out):
        while i < len(out) and out[i].isspace():
            i += 1
        if i >= len(out):
            break
        arr, i = dec.raw_decode(out, i)
        items += arr
    return items


class GhIssues:
    def __init__(self, repo="scala/bug", name="bug"):
        self.repo, self.name = repo, name

    def _chunks(self, id_prefix, doc, title, text, url, meta):
        for n, part in enumerate(_split_big((text or "").splitlines(), MAX)):
            body = "\n".join(part).strip()
            if body or n == 0:
                yield Chunk(f"{self.name}:{id_prefix}" + (f"~{n}" if n else ""), doc, title, body, url, meta)

    def sync(self, store, since=None, limit=None, log=print):
        since = since or store.get(self.name, "since", DEFAULT_SINCE)
        iss = gh_pages(f"repos/{self.repo}/issues", state="all", sort="updated", direction="asc", since=since)
        iss = [i for i in iss if "pull_request" not in i][:limit]
        titles = {}
        tot = [0, 0, 0, 0]
        for i in iss:
            n = i["number"]
            titles[n] = i["title"]
            labels = [l["name"] for l in i["labels"]]
            chunks = list(self._chunks(f"issue:{n}", f"issue:{n}", f"{self.repo}#{n} {i['title']}", i["body"], i["html_url"],
                                       {"state": i["state"], "labels": labels, "number": n, "kind": "issue", "updated": i["updated_at"]}))
            for k, v in enumerate(store.apply(self.name, chunks)):
                tot[k] += v
        cur = max([i["updated_at"] for i in iss] or [since])
        coms = gh_pages(f"repos/{self.repo}/issues/comments", sort="updated", direction="asc", since=since)[:limit]
        for c in coms:
            n = int(c["issue_url"].rsplit("/", 1)[1])
            if n not in titles:
                r = store.db.execute("SELECT title FROM chunks WHERE id=?", (f"{self.name}:issue:{n}",)).fetchone()
                titles[n] = r[0].split(" ", 1)[1] if r else ""
            chunks = list(self._chunks(f"comment:{c['id']}", f"issue:{n}", f"{self.repo}#{n} {titles[n]}  (comment by {c['user']['login']})",
                                       c["body"], c["html_url"], {"kind": "comment", "number": n, "updated": c["updated_at"]}))
            for k, v in enumerate(store.apply(self.name, chunks)):
                tot[k] += v
            cur = max(cur, c["updated_at"])
        store.put(self.name, "since", cur)
        store.commit()
        log(f"{self.name} since {since}: {len(iss)} issues, {len(coms)} comments touched -> +{tot[0]} ~{tot[1]} -{tot[2]} ={tot[3]} chunks")

    def reconcile(self, store, log=print):
        live = {i["number"] for i in gh_pages(f"repos/{self.repo}/issues", state="all") if "pull_request" not in i}
        gone = [r[0] for r in store.db.execute("SELECT DISTINCT doc FROM chunks WHERE source=?", (self.name,))
                if int(r[0].split(":")[1]) not in live]
        # only issues inside the backfill horizon are expected to be indexed, so only removals are acted on
        n = sum(store.delete_doc(self.name, d) for d in gone)
        store.commit()
        log(f"{self.name} reconcile: {len(live)} live issues, {len(gone)} issues removed ({n} chunks)")
